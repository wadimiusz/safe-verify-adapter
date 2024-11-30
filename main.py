import os.path
import shutil
import subprocess
import threading
import time
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
    stdout: str | None = None
    stderr: str | None = None


class SyntaxCheckRequest(BaseModel):
    file_content: str


class SyntaxCheckResponse(BaseModel):
    success: bool
    message: str
    stdout: str | None = None
    stderr: str | None = None


def run_command(args: list[str], timeout: float | int, working_dir: str):
    result = subprocess.run(
        args, text=True, cwd=working_dir, capture_output=True, timeout=timeout
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Command {args} failed.\n"
            f"Stdout:\n{result.stdout}\n\n\n"
            f"Stderr:\n{result.stderr}\n\n\n"
        )


class Verificator:

    def __init__(self):
        self.update_lock = threading.Lock()
        self.working_dir = os.path.abspath(os.environ["WORKING_DIR"])
        os.makedirs(self.working_dir, exist_ok=True)
        logger.info(f"The working dir will be {self.working_dir}")
        self.safe_verify_dir = os.path.join(self.working_dir, "SafeVerify")
        if not os.path.exists(self.safe_verify_dir):
            run_command(
                ["git", "clone", "https://github.com/GasStationManager/SafeVerify.git"],
                working_dir=self.working_dir,
                timeout=60,
            )

        with open(os.path.join(self.safe_verify_dir, "lean-toolchain")) as f:
            toolchain = f.read().strip()

        logger.info("Installing toolchain.")
        run_command(
            ["elan", "toolchain", "install", toolchain],
            timeout=600,
            working_dir=self.safe_verify_dir,
        )

        logger.info("Toolchain installed. Setting as default.")
        run_command(
            ["elan", "default", toolchain], timeout=10, working_dir=self.safe_verify_dir
        )

        logger.info("Default toolchain set. Updating lake dependencies.")
        run_command(
            ["lake", "update"],
            working_dir=self.safe_verify_dir,
            timeout=1200,
        )
        logger.info("Lake dependencies updated.")

        assert os.path.isdir(self.safe_verify_dir)
        self.update_repo_thread = threading.Thread(
            target=self.update_repo_thread_function
        )
        self.update_repo_thread.start()
        self.app = FastAPI()
        self.app.add_event_handler("shutdown", self.shutdown)
        self.setup_routes()

    def update_repo_thread_function(self):
        while True:
            with self.update_lock:
                run_command(
                    args=["git", "pull"], working_dir=self.safe_verify_dir, timeout=120
                )
                run_command(
                    args=["lake", "update"],
                    timeout=1200,
                    working_dir=self.safe_verify_dir,
                )
                logger.info("Successfully updated. Back to sleep...")

            time.sleep(60 * 60)

    def shutdown(self):
        shutil.rmtree(self.working_dir)

    def compile(
        self, input_path: str, output_path: str, content: str
    ) -> tuple[int, str, str]:
        with open(input_path, "w") as f:
            f.write(content)

        result = subprocess.run(
            f"lean -o {output_path} {input_path}",
            cwd=self.safe_verify_dir,
            capture_output=True,
            text=True,
            timeout=600,
            shell=True,
        )

        return result.returncode, result.stdout, result.stderr

    def setup_routes(self):
        @self.app.post("/check-syntax", response_model=SyntaxCheckResponse)
        def check_syntax(request: SyntaxCheckRequest):
            with self.update_lock:
                file_content = request.file_content

                file_uncompiled_path = os.path.join(self.safe_verify_dir, "File.lean")
                file_compiled_path = os.path.join(self.safe_verify_dir, "File.olean")

                try:
                    exit_code, stdout, stderr = self.compile(
                        input_path=file_uncompiled_path,
                        output_path=file_compiled_path,
                        content=file_content,
                    )
                except Exception:
                    try:
                        os.remove(file_uncompiled_path)
                        os.remove(file_compiled_path)
                    except FileNotFoundError:
                        pass

                    return SyntaxCheckResponse(
                        success=False,
                        message="Running the syntax check resulted in an exception.",
                        stdout=None,
                        stderr=traceback.format_exc(),
                    )

                try:
                    os.remove(file_uncompiled_path)
                    os.remove(file_uncompiled_path)
                except FileNotFoundError:
                    pass

                if exit_code == 0:
                    success = True
                    message = "Syntax check passed."
                else:
                    success = False
                    message = f"Syntax check failed (exit code {exit_code} received)."

                return SyntaxCheckResponse(
                    success=success,
                    message=message,
                    stdout=stdout,
                    stderr=stderr,
                )

        @self.app.post("/verify-proof", response_model=ProofResponse)
        def verify_proof(request: ProofRequest):
            with self.update_lock:
                theorem = request.theorem
                proof = request.proof

                theorem_uncompiled_path = os.path.join(
                    self.safe_verify_dir, "Theorem.lean"
                )
                theorem_compiled_path = os.path.join(
                    self.safe_verify_dir, "Theorem.olean"
                )

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
                        message="Failed to compile the theorem file.",
                        stdout=stdout,
                        stderr=stderr,
                    )

                proof_uncompiled_path = os.path.join(self.safe_verify_dir, "Proof.lean")
                proof_compiled_path = os.path.join(self.safe_verify_dir, "Proof.olean")

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
                        f"lake env lean --run Main.lean {theorem_compiled_path} {proof_compiled_path}",
                        cwd=self.safe_verify_dir,
                        capture_output=True,
                        text=True,
                        timeout=600,
                        shell=True,
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


verificator = Verificator()
app = verificator.app
