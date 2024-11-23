import os.path
import secrets
import shlex
import shutil
import subprocess
import traceback

from fastapi import FastAPI, HTTPException
from loguru import logger
from pydantic import BaseModel


class ProofRequest(BaseModel):
    theorem: str
    proof: str


class ProofResponse(BaseModel):
    success: bool
    message: str
    stdout: str = None
    stderr: str = None


class VerificationApp:
    def __init__(self):
        self.working_dir = f"/tmp/{secrets.token_hex()}"
        logger.info(f"The working dir will be {self.working_dir}")
        result = subprocess.run(
            ["git", "clone", "https://github.com/GasStationManager/SafeVerify.git"],
            cwd=self.working_dir,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to clone the SafeVerify git repo. Exit code: {result.returncode}. "
                f"Stdout:\n{result.stdout}\n\n"
                f"Stderr:\n{result.stderr}\n\n"
            )

        self.safe_verify_dir = os.path.join(self.working_dir, "SafeVerify")
        assert os.path.isdir(self.safe_verify_dir)
        self.app = FastAPI()
        self.app.add_event_handler("shutdown", self.shutdown)

    def shutdown(self):
        shutil.rmtree(self.working_dir)

    def compile(
        self, input_path: str, output_path: str, content: str
    ) -> tuple[int, str, str]:
        with open(input_path, "w") as f:
            f.write(content)

        result = subprocess.run(
            ["lake", "env", "run", "-o", shlex.quote(output_path), input_path],
            cwd=self.safe_verify_dir,
            capture_output=True,
            text=True,
            timeout=60,
        )

        return result.returncode, result.stdout, result.stderr

    def setup_routes(self):
        @self.app.post("/verify-proof", response_model=ProofResponse)
        def verify_proof(request: ProofRequest):
            theorem = request.theorem
            proof = request.proof

            theorem_uncompiled_path = os.path.join(self.working_dir, "Theorem.lean")
            theorem_compiled_path = os.path.join(self.working_dir, "Theorem.olean")

            try:
                exit_code, stdout, stderr = self.compile(
                    input_path=theorem_uncompiled_path,
                    output_path=theorem_compiled_path,
                    content=theorem,
                )
            except Exception:
                return ProofResponse(
                    success=False,
                    message="Trying to compile the theorem file yielded an exception.",
                    stdout=None,
                    stderr=traceback.format_exc(),
                )

            os.remove(theorem_uncompiled_path)

            if exit_code != 0:
                return ProofResponse(
                    success=False,
                    messages="Failed to compile the theorem file.",
                    stdout=stdout,
                    stderr=stderr,
                )

            proof_uncompiled_path = os.path.join(self.working_dir, "Proof.lean")
            proof_compiled_path = os.path.join(self.working_dir, "Proof.olean")

            try:
                exit_code, stdout, stderr = self.compile(
                    input_path=proof_uncompiled_path,
                    output_path=proof_compiled_path,
                    content=proof,
                )
            except Exception:
                return ProofResponse(
                    success=False,
                    message="Trying to compile the proof file yielded an exception.",
                    stdout=None,
                    stderr=traceback.format_exc(),
                )

            os.remove(proof_uncompiled_path)

            if exit_code != 0:
                return ProofResponse(
                    success=False,
                    messages="Failed to compile the proof file.",
                    stdout=stdout,
                    stderr=stderr,
                )

            try:
                result = subprocess.run(
                    [
                        "lean",
                        "env",
                        "lean",
                        "--run",
                        "Main.lean",
                        theorem_compiled_path,
                        proof_compiled_path,
                    ],
                    cwd=self.safe_verify_dir,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )

                if result.returncode == 0:

                    logger.info(
                        f"======================\nTheorem: \n{theorem}\n\nProof:\n{proof}\n\n"
                        f"Result: accepted.\n"
                    )
                    return ProofResponse(
                        success=True,
                        message="Proof is valid.",
                        stdout=result.stdout,
                        stderr=result.stderr,
                    )
                else:
                    logger.info(
                        f"======================\nTheorem: \n{theorem}\n\nProof:\n{proof}\n\n"
                        f"Result: declined.\n"
                    )
                    return ProofResponse(
                        success=False,
                        message="Proof is invalid.",
                        output=result.stdout,
                        stderr=result.stderr,
                    )
            except subprocess.TimeoutExpired:
                raise HTTPException(
                    status_code=504, detail="Proof verification timed out."
                )
            except Exception:
                raise HTTPException(status_code=500, detail=traceback.format_exc())
