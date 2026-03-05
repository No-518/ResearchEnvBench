import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any


def _now() -> float:
    return time.time()


def _mkdir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


def run_cmd(
    cmd: List[str],
    timeout_sec: Optional[int] = None,
    check: bool = True,
    capture: bool = True,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[int, str, str]:
    """
    Returns: (returncode, stdout, stderr)
    """
    p = subprocess.run(
        cmd,
        timeout=timeout_sec,
        check=False,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=capture,
    )
    if check and p.returncode != 0:
        raise RuntimeError(
            f"Command failed (rc={p.returncode}):\n"
            f"{' '.join(shlex.quote(x) for x in cmd)}\n"
            f"--- stdout ---\n{p.stdout}\n"
            f"--- stderr ---\n{p.stderr}\n"
        )
    return p.returncode, p.stdout or "", p.stderr or ""


def sanitize_docker_inspect(inspect_obj: Any) -> Any:
    """
    Remove or mask sensitive env values from `docker inspect` JSON.
    """
    def _mask_env_list(env_list: List[str]) -> List[str]:
        masked = []
        for item in env_list:
            if "=" not in item:
                masked.append(item)
                continue
            k, _v = item.split("=", 1)
            uk = k.upper()
            if any(s in uk for s in ["KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH", "BEARER"]):
                masked.append(f"{k}=<redacted>")
            else:
                # keep non-sensitive env (JOB_ID etc.) visible
                masked.append(item)
        return masked

    if isinstance(inspect_obj, list) and inspect_obj:
        obj = inspect_obj[0]
        if isinstance(obj, dict):
            cfg = obj.get("Config", {})
            if isinstance(cfg, dict) and isinstance(cfg.get("Env"), list):
                cfg["Env"] = _mask_env_list(cfg["Env"])
            obj["Config"] = cfg
        return [obj]
    return inspect_obj


@dataclass
class DockerRunSpec:
    image: str
    name: str
    shm_size: str = "16g"
    network: str = "host"  # could be "bridge" / "none"
    gpus: Optional[str] = "all"  # "all" or "device=0" or "device=0,1" or None
    env: Dict[str, str] = None
    env_file: Optional[str] = None
    labels: Dict[str, str] = None
    volumes: List[Tuple[str, str, str]] = None  # (host_path, container_path, mode)
    workdir: str = "/data/project"
    command: str = "mkdir -p /data/project /data/results /opt/scimlopsbench && sleep infinity"


class DockerClient:
    def __init__(self, log_path: Optional[str] = None):
        self.log_path = log_path

    def _log(self, msg: str) -> None:
        if not self.log_path:
            return
        _mkdir(os.path.dirname(self.log_path))
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")

    def docker(self, args: List[str], timeout_sec: Optional[int] = None, check: bool = True) -> Tuple[int, str, str]:
        cmd = ["docker"] + args
        self._log("+ " + " ".join(shlex.quote(x) for x in cmd))
        return run_cmd(cmd, timeout_sec=timeout_sec, check=check, capture=True)

    def run_detached(self, spec: DockerRunSpec) -> str:
        args = ["run", "-d", "--name", spec.name, "--shm-size", spec.shm_size, "--network", spec.network]
        if spec.gpus:
            args += ["--gpus", spec.gpus]
        if spec.env:
            for k, v in spec.env.items():
                args += ["-e", f"{k}={v}"]
        if spec.env_file:
            args += ["--env-file", spec.env_file]
        if spec.labels:
            for k, v in spec.labels.items():
                args += ["--label", f"{k}={v}"]
        if spec.volumes:
            for hp, cp, mode in spec.volumes:
                args += ["-v", f"{hp}:{cp}:{mode}"]
        args += ["-w", spec.workdir]
        args += [spec.image, "bash", "-lc", spec.command]
        _rc, out, _err = self.docker(args)
        cid = out.strip()
        if not cid:
            raise RuntimeError("docker run did not return container id")
        return cid

    def exec_bash(
        self,
        cid: str,
        bash_cmd: str,
        timeout_sec: Optional[int] = None,
        check: bool = True,
        user: Optional[str] = None,
    ) -> Tuple[int, str, str]:
        # Use bash -lc so conda init / profile can load if needed
        args = ["exec"]
        if user:
            args += ["--user", user]
        args += [cid, "bash", "-lc", bash_cmd]
        return self.docker(args, timeout_sec=timeout_sec, check=check)

    def cp_to(self, cid: str, host_src: str, container_dst: str) -> None:
        self.docker(["cp", host_src, f"{cid}:{container_dst}"], check=True)

    def inspect(self, cid: str) -> Any:
        _rc, out, _err = self.docker(["inspect", cid], check=True)
        return json.loads(out)

    def image_id(self, image: str) -> str:
        _rc, out, _err = self.docker(["image", "inspect", image, "--format", "{{.Id}}"], check=True)
        return out.strip()

    def remove(self, cid: str, force: bool = True) -> None:
        args = ["rm"]
        if force:
            args.append("-f")
        args.append(cid)
        self.docker(args, check=False)
