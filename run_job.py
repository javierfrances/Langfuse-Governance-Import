#!/usr/bin/env python3
"""Code Engine Job 2: COS inventory (read-only) -> watsonx Governance objects.

Uses the *same* validated SaaS importer as WSL. Never updates COS; does not
read Langfuse directly. Check-only by default; explicit IMPORT_MODE=apply to write.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

DEFAULT_COS_KEY = "inventario_langfuse.json"
PROBLEM_STATES = {"name_conflict", "association_mismatch", "association_unverified", "error"}
EXPECTED_STATES = {"created", "already_exists", "would_create"} | PROBLEM_STATES


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Falta la variable obligatoria {name}")
    return value


def https_url(name: str) -> str:
    value = required_env(name).rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{name} debe ser una URL HTTPS válida sin credenciales ni parámetros")
    return value


def read_config() -> dict:
    mode = os.getenv("IMPORT_MODE", "check").strip().lower()
    if mode not in ("check", "apply"):
        raise ValueError("IMPORT_MODE debe ser check o apply")
    max_agents_raw = os.getenv("IMPORT_MAX_AGENTS", "0").strip()
    try:
        max_agents = int(max_agents_raw)
    except ValueError as exc:
        raise ValueError("IMPORT_MAX_AGENTS debe ser un entero >= 0") from exc
    if max_agents < 0:
        raise ValueError("IMPORT_MAX_AGENTS debe ser >= 0")

    cfg = {
        "cos_endpoint": https_url("COS_ENDPOINT"),
        "cos_key": required_env("COS_API_KEY"),
        "cos_service_instance_id": required_env("COS_SERVICE_INSTANCE_ID"),
        "cos_bucket": required_env("COS_BUCKET"),
        "cos_object_key": os.getenv("COS_OBJECT_KEY", DEFAULT_COS_KEY).strip() or DEFAULT_COS_KEY,
        "op_base_url": https_url("OP_BASE_URL"),
        "op_api_key": required_env("OP_API_KEY"),
        "parent_id": os.getenv("OP_PRIMARY_PARENT_ID", "").strip(),
        "mode": mode,
        "only_agent": os.getenv("IMPORT_ONLY_AGENT", "").strip(),
        "max_agents": max_agents,
    }
    if mode == "apply" and not cfg["parent_id"]:
        raise ValueError("IMPORT_MODE=apply requiere OP_PRIMARY_PARENT_ID (Resource ID del objeto padre para nuevos agentes)")
    return cfg


def make_cos_client(cfg: dict):
    try:
        import ibm_boto3
        from ibm_botocore.client import Config
    except ImportError as exc:
        raise RuntimeError("Falta ibm-cos-sdk; instalar requirements.txt") from exc
    return ibm_boto3.client(
        "s3",
        ibm_api_key_id=cfg["cos_key"],
        ibm_service_instance_id=cfg["cos_service_instance_id"],
        config=Config(signature_version="oauth"),
        endpoint_url=cfg["cos_endpoint"],
    )


def download_inventory(client, bucket: str, key: str, dest: Path) -> None:
    # A missing object is a failure; this job never creates a new inventory.
    response = client.get_object(Bucket=bucket, Key=key)
    raw = response["Body"].read(10 * 1024 * 1024 + 1)
    if len(raw) > 10 * 1024 * 1024:
        raise ValueError("El JSON de COS supera 10 MiB; se cancela la importación")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("El JSON de COS debe ser un objeto")
    source = payload.get("source")
    if not isinstance(source, dict) or source.get("platform") != "Langfuse":
        raise ValueError("El JSON de COS no parece un inventario Langfuse (source.platform)")
    inv = payload.get("inventory")
    if not isinstance(inv, dict) or not isinstance(inv.get("agents"), list) or not inv["agents"]:
        raise ValueError("El JSON de COS no contiene inventory.agents como lista no vacía")
    dest.write_bytes(raw)
    print(f"Inventario recuperado de COS: {bucket}/{key}; agentes={len(inv['agents'])}", flush=True)


def build_command(cfg: dict, input_path: Path, report_path: Path) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).with_name("importar_agentes_y_tools.py")),
        "--input", str(input_path),
        "--report", str(report_path),
        "--check" if cfg["mode"] == "check" else "--apply",
    ]
    if cfg["parent_id"]:
        command.extend(("--parent-id", cfg["parent_id"]))
    if cfg["only_agent"]:
        command.extend(("--only-agent", cfg["only_agent"]))
    if cfg["max_agents"]:
        command.extend(("--max-agents", str(cfg["max_agents"])))
    return command


def report_summary(report_path: Path, *, fail_on_warning: bool = True) -> None:
    if not report_path.exists():
        raise RuntimeError("El importador no generó el fichero de resultados")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or not isinstance(report.get("results"), list) or not report["results"]:
        raise ValueError("El informe del importador está vacío o no contiene results válidos")
    tally = {}
    anomalies = []
    for agent in report["results"]:
        entries = [("agent", agent)]
        entries += [("tool", x) for x in agent.get("tools", [])]
        entries += [("model", x) for x in agent.get("models", [])]
        for typ, item in entries:
            status = str(item.get("status", "unknown"))
            tally[(typ, status)] = tally.get((typ, status), 0) + 1
            if status not in EXPECTED_STATES or status in PROBLEM_STATES:
                anomalies.append((typ, item.get("agent") or item.get("tool") or item.get("model"), status))
    print("RESUMEN DE IMPORTACIÓN:", flush=True)
    for (typ, status), count in sorted(tally.items()):
        print(f"  {typ}: {status} = {count}", flush=True)
    if anomalies:
        print("ATENCIÓN: conflictos o asociaciones sin confirmar:", flush=True)
        for typ, name, status in anomalies:
            print(f"  {typ} {name}: {status}", flush=True)
        if fail_on_warning:
            raise RuntimeError("El importador detectó conflictos o asociaciones sin verificar; revisar logs antes de continuar")


def main() -> int:
    cfg = read_config()
    print("Job 2: inventario Langfuse en COS -> Consola de Gobierno de watsonx", flush=True)
    print(
        f"Modo={cfg['mode']}; only_agent={cfg['only_agent'] or 'TODOS'}; "
        f"max_agents={cfg['max_agents'] or 'TODOS'}; COS READ-ONLY", flush=True,
    )
    client = make_cos_client(cfg)
    with tempfile.TemporaryDirectory(prefix="governance-import-") as folder:
        input_path = Path(folder) / "inventario_langfuse.json"
        report_path = Path(folder) / "resultado_importacion.json"
        download_inventory(client, cfg["cos_bucket"], cfg["cos_object_key"], input_path)
        command = build_command(cfg, input_path, report_path)
        # Inherits OP_API_KEY/OP_BASE_URL from Code Engine Secret/env. The original
        # importer uses IAM bearer tokens and retries are safe by Langfuse markers.
        subprocess.run(command, check=True)
        report_summary(report_path)
    print("Job finalizado. No se ha modificado el inventario de COS.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Never echo key values. Common SDK exception messages contain COS bucket
        # names, not API keys. Keep errors short and actionable.
        print(f"ERROR JOB 2: {type(exc).__name__}: {str(exc)[:500]}", file=sys.stderr, flush=True)
        raise SystemExit(1)
