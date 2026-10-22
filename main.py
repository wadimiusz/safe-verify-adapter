import os.path
import shutil
import subprocess
import threading
import time
import traceback
from contextlib import asynccontextmanager

import requests
from fastapi import FastAPI, HTTPException
from loguru import logger
from pydantic import BaseModel


def format_duration(duration_in_seconds):
    hours = int(duration_in_seconds // 3600)
    minutes = int((duration_in_seconds % 3600) // 60)
    seconds = duration_in_seconds % 60
    return f"{hours}:{minutes:02d}:{seconds:05.2f}"


class ProofRequest(BaseModel):
    theorem: str
    proof: str


class ProofResponse(BaseModel):
    success: bool
    message: str
    stdout: str | None = None
    stderr: str | None = None


class SyntaxCheckRequest(BaseModel):
    code: str


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
        result = subprocess.run(
            ["elan", "toolchain", "install", toolchain],
            text=True, cwd=self.safe_verify_dir, capture_output=True, timeout=600,
        )
        if result.returncode != 0 and "already installed" not in result.stderr:
            raise RuntimeError(
                f"Failed to install toolchain.\nStdout:\n{result.stdout}\nStderr:\n{result.stderr}"
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
        # daemon: if the app fails to start, the process must exit so docker sees the crash
        self.update_repo_thread = threading.Thread(
            target=self.update_repo_thread_function, daemon=True
        )
        self.update_repo_thread.start()

        @asynccontextmanager
        async def lifespan(app):
            yield
            self.shutdown()

        self.app = FastAPI(lifespan=lifespan)
        self.setup_routes()
        self.close_bounty_url = "http://theorem-marketplace:5000/api/close_bounty"

    def close_bounty(self, theorem: str, proof: str):
        try:
            response = requests.post(
                self.close_bounty_url,
                json={"theorem": theorem, "proof": proof},
                timeout=20,
            )
        except Exception:
            logger.warning(
                f"Failed to close bounty for the following theorem: \n{theorem}\n\n"
                f"The provided proof was:\n{proof}\n\nTraceback:\n:{traceback.format_exc()}"
            )
            return

        if response.status_code != 200:
            logger.warning(
                f"Failed to close bounty (status code {response.status_code}, text {response.text}).\n"
                f"Theorem:\n{theorem}\n\nProof:\n{proof}"
            )

    def update_repo_thread_function(self):
        while True:
            # __init__ has just run lake update; updating again right away would block
            # checks for ~2 minutes after every start.
            time.sleep(60 * 60)
            with self.update_lock:
                start = time.time()
                run_command(
                    args=["git", "pull"], working_dir=self.safe_verify_dir, timeout=120
                )
                run_command(
                    args=["lake", "update"],
                    timeout=1200,
                    working_dir=self.safe_verify_dir,
                )
                logger.info(
                    f"Successfully updated. This took {format_duration(time.time() - start)}. "
                    f"Back to sleep..."
                )

    def shutdown(self):
        shutil.rmtree(self.working_dir)

    def compile(
        self, input_path: str, output_path: str, content: str
    ) -> tuple[int, str, str]:
        with open(input_path, "w") as f:
            f.write(content)

        result = subprocess.run(
            f"lake env lean -o {output_path} {input_path}",
            cwd=self.safe_verify_dir,
            capture_output=True,
            text=True,
            timeout=600,
            shell=True,
        )

        return result.returncode, result.stdout, result.stderr

    def add_imports_to_main(
        self, input_path: str, output_path: str, imports: list[str]
    ):
        with open(input_path) as f:
            content = f.read()

        lines = set(content.split("\n"))
        imports_except_already_used = [
            name for name in imports if f"import {name}" not in lines
        ]

        with open(output_path, "w") as f:
            for name in imports_except_already_used:
                f.write(f"import {name}\n")

            f.write(content)

    def setup_routes(self):
        @self.app.post("/check-syntax", response_model=SyntaxCheckResponse)
        def check_syntax(request: SyntaxCheckRequest):
            logger.debug(f"Request: {request}")
            with self.update_lock:
                code = request.code

                file_uncompiled_path = os.path.join(self.safe_verify_dir, "File.lean")
                file_compiled_path = os.path.join(self.safe_verify_dir, "File.olean")

                try:
                    exit_code, stdout, stderr = self.compile(
                        input_path=file_uncompiled_path,
                        output_path=file_compiled_path,
                        content=code,
                    )
                except Exception:
                    logger.error(traceback.format_exc())
                    try:
                        os.remove(file_uncompiled_path)
                        os.remove(file_compiled_path)
                    except FileNotFoundError:
                        pass
                    logger.info(
                        f"======================\nChecking syntax for the file:\n"
                        f"{code}\n"
                        f"Result: declined."
                    )
                    return SyntaxCheckResponse(
                        success=False,
                        message="Running the syntax check resulted in an exception.",
                        stdout=None,
                        stderr=traceback.format_exc(),
                    )

                try:
                    os.remove(file_uncompiled_path)
                    os.remove(file_compiled_path)
                except FileNotFoundError:
                    pass

                if exit_code == 0:
                    logger.info(
                        f"======================\nChecking syntax for the file:\n"
                        f"{code}\n"
                        f"Result: accepted."
                    )
                    success = True
                    message = "Syntax check passed."
                else:
                    logger.info(
                        f"======================\nChecking syntax for the file:\n"
                        f"{code}\n"
                        f"Result: declined."
                    )
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
                        message="Failed to compile the proof file.",
                        stdout=stdout,
                        stderr=stderr,
                    )

                try:
                    self.add_imports_to_main(
                        input_path=os.path.join(self.safe_verify_dir, "Main.lean"),
                        output_path=os.path.join(
                            self.safe_verify_dir, "Main_with_imports.lean"
                        ),
                        imports=["Mathlib"],
                    )
                except Exception:
                    raise HTTPException(status_code=500, detail=traceback.format_exc())

                try:
                    result = subprocess.run(
                        f"lake env lean --run Main_with_imports.lean {theorem_compiled_path} {proof_compiled_path}",
                        cwd=self.safe_verify_dir,
                        capture_output=True,
                        text=True,
                        timeout=600,
                        shell=True,
                    )

                    if result.returncode == 0:
                        self.close_bounty(theorem, proof)
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
                            stdout=result.stdout,
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
