"""Run the local agent for one phase of one job.

The agent is a headless Claude Code process pointed at a local model server
(Ollama serves the Anthropic Messages API), given no built-in tools: its
whole world is the printq MCP server, restricted to the tools of the phase.

The agent's answer is not trusted. The caller looks at the job's state after
the run; if it is not where the phase should have left it, the job is
escalated to the operator.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import config, jobs

PHASES = {
    # phase: (tools offered, states that count as a finished phase)
    "prepare": ("get_job,slice_job,printer_status,announce_job,escalate", ("queued", "needs_human")),
    "launch": ("get_job,printer_status,check_bed,upload_job,start_job,escalate",
               ("printing", "awaiting_go", "needs_human", "queued")),
}


def run(phase: str, job_id: str, timeout: int = 1500) -> dict:
    """Run one phase. Returns {ok, state, text}; ok means the job ended in a state the phase allows."""
    tool_names, good_states = PHASES[phase]
    job_path = jobs.job_dir(job_id)
    home = config.DATA / "agent-home"
    home.mkdir(parents=True, exist_ok=True)
    mcp = job_path / f"mcp-{phase}.json"
    mcp.write_text(json.dumps({"mcpServers": {"printq": {
        "command": sys.executable, "args": ["-m", "printq.mcp_server"],
        "env": {"PRINTQ_MCP_TOOLS": tool_names, "PRINTQ_HOME": str(config.HOME), "PRINTQ_DATA": str(config.DATA),
                "PYTHONPATH": str(config.REPO)}}}}))
    prompt = (config.REPO / "prompts" / f"{phase}.md").read_text().replace("{job_id}", job_id)
    cmd = [config.env("PRINTQ_AGENT_BIN", "claude"), "--bare", "-p", prompt,
           "--model", config.AGENT_MODEL, "--tools", "", "--mcp-config", str(mcp), "--strict-mcp-config",
           "--allowedTools", *[f"mcp__printq__{n}" for n in tool_names.split(",")],
           "--max-turns", "14", "--output-format", "json"]
    env = {**os.environ, "ANTHROPIC_BASE_URL": config.OLLAMA_URL, "ANTHROPIC_API_KEY": "ollama",
           "CLAUDE_CONFIG_DIR": str(home), "MCP_TOOL_TIMEOUT": "1800000"}
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    started = time.time()
    try:
        proc = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout, cwd=job_path)
        raw = proc.stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        raw = json.dumps({"result": f"agent did not run to completion: {exc}"})
    (job_path / f"agent-{phase}-{time.strftime('%H%M%S')}.json").write_text(raw)
    try:
        out = json.loads(raw)
        text = out.get("result") or ""
        turns = out.get("num_turns")
    except ValueError:
        text, turns = raw[-500:], None
    state = jobs.state(job_id)["state"]
    jobs.event(job_id, f"agent {phase}: {turns} turns, {time.time() - started:.0f} s, ended in '{state}'")
    return {"ok": state in good_states, "state": state, "text": text.strip()}
