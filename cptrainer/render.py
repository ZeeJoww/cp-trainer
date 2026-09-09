from __future__ import annotations

from datetime import datetime
from html import escape
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from .db import Database, session_ended


def _fmt_epoch(value: int | None, timezone: str) -> str:
    if value is None:
        return "—"
    return datetime.fromtimestamp(value, ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M")


def _label(ordinal: int) -> str:
    if 1 <= ordinal <= 26:
        return chr(ord("A") + ordinal - 1)
    return str(ordinal)


def export_session_markdown(
    db: Database,
    session_id: int,
    destination: Path,
    *,
    timezone: str,
    reveal: bool = False,
) -> Path:
    session = db.get_session(session_id)
    items = db.get_session_items(session_id)
    reveal = session_ended(session) and reveal
    lines = [
        f"# {session['name']}",
        "",
        f"- Session: `{session_id}`",
        f"- Type: `{session['kind']}` / `{session['platform']}`",
        f"- Status: `{session['status']}`",
        f"- Scheduled: {_fmt_epoch(session['scheduled_at'], timezone)}",
        f"- Duration: {session['duration_minutes']} minutes",
        f"- Rated locally: {'yes' if session['rated'] else 'no'}",
        "",
        "| # | Platform | Problem | Result | Time | Wrong | Difficulty | Target |",
        "|---:|:--:|---|:--:|---:|---:|---:|---:|",
    ]
    for row in items:
        result = "AC" if row["solved"] else "—"
        elapsed = (
            "—"
            if row["elapsed_seconds"] is None
            else f"{row['elapsed_seconds'] / 60:.1f}m"
        )
        difficulty = f"{row['difficulty']:.0f}" if reveal else "hidden"
        target = f"{row['target_difficulty']:.0f}" if reveal else "hidden"
        title = str(row["title"]).replace("|", "\\|")
        lines.append(
            f"| {_label(int(row['ordinal']))} | {row['platform']} | "
            f"[{title}]({row['url']}) | {result} | {elapsed} | "
            f"{row['wrong_attempts']} | {difficulty} | {target} |"
        )
    if session["status"] == "finished":
        lines.extend(
            [
                "",
                "## Result",
                "",
                f"- Score: **{session['score']:.2f}**" if session["score"] is not None else "- Score: —",
                f"- Penalty: **{session['penalty_minutes']:.2f} min**"
                if session["penalty_minutes"] is not None
                else "- Penalty: —",
            ]
        )
        if session["performance"] is not None:
            lines.extend(
                [
                    f"- Local performance: **{session['performance']:.0f}**",
                    f"- Local rating: **{session['old_rating']:.0f} → {session['new_rating']:.0f}**",
                ]
            )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return destination


def export_session_html(
    db: Database,
    session_id: int,
    destination: Path,
    *,
    timezone: str,
    reveal: bool = False,
) -> Path:
    session = db.get_session(session_id)
    items = db.get_session_items(session_id)
    reveal = session_ended(session) and reveal
    tags_available = session_ended(session)
    start_ms = int(session["started_at"] or 0) * 1000
    duration_ms = int(session["duration_minutes"]) * 60 * 1000
    rows: list[str] = []
    for row in items:
        title = escape(str(row["title"]))
        url = escape(str(row["url"]), quote=True)
        result = "AC" if row["solved"] else "—"
        elapsed = (
            "—"
            if row["elapsed_seconds"] is None
            else f"{row['elapsed_seconds'] / 60:.1f} min"
        )
        difficulty = f"{row['difficulty']:.0f}" if reveal else "hidden"
        target = f"{row['target_difficulty']:.0f}" if reveal else "hidden"
        tags = ", ".join(json.loads(row["tags_json"] or "[]")) if tags_available else ""
        rows.append(
            "<tr>"
            f"<td class='slot'>{_label(int(row['ordinal']))}</td>"
            f"<td>{escape(str(row['platform']))}</td>"
            f"<td><a href='{url}' target='_blank' rel='noreferrer'>{title}</a></td>"
            f"<td>{result}</td><td>{elapsed}</td><td>{row['wrong_attempts']}</td>"
            f"<td>{difficulty}</td><td>{target}</td>" + (f"<td class='tags' hidden>{escape(tags)}</td>" if tags_available else "") +
            "</tr>"
        )
    result_card = ""
    if session["status"] == "finished":
        rating_line = ""
        if session["performance"] is not None:
            rating_line = (
                f"<div><b>Performance</b><span>{session['performance']:.0f}</span></div>"
                f"<div><b>Local rating</b><span>{session['old_rating']:.0f} → {session['new_rating']:.0f}</span></div>"
            )
        result_card = f"""
        <section class="result-card">
          <div><b>Score</b><span>{float(session['score'] or 0):.2f}</span></div>
          <div><b>Penalty</b><span>{float(session['penalty_minutes'] or 0):.2f} min</span></div>
          {rating_line}
        </section>
        """
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(str(session['name']))}</title>
<style>
:root {{ color-scheme: light dark; font-family: ui-sans-serif, system-ui, sans-serif; }}
body {{ margin: 0; padding: 2rem; max-width: 1200px; margin-inline: auto; }}
h1 {{ margin-bottom: .3rem; }}
.meta {{ display: flex; flex-wrap: wrap; gap: .7rem 1.3rem; opacity: .85; }}
.timer {{ font-variant-numeric: tabular-nums; font-size: 2.2rem; font-weight: 750; margin: 1.5rem 0; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 1rem; }}
th, td {{ text-align: left; padding: .7rem .55rem; border-bottom: 1px solid color-mix(in srgb, currentColor 18%, transparent); }}
.slot {{ font-weight: 750; }}
a {{ color: inherit; text-decoration-thickness: .08em; text-underline-offset: .2em; }}
.result-card {{ display: flex; flex-wrap: wrap; gap: 1rem; margin: 1rem 0; }}
.result-card div {{ border: 1px solid color-mix(in srgb, currentColor 24%, transparent); border-radius: .7rem; padding: .8rem 1rem; min-width: 9rem; }}
.result-card b, .result-card span {{ display: block; }}
.result-card span {{ font-size: 1.3rem; margin-top: .25rem; }}
.note {{ opacity: .72; margin-top: 1.2rem; }}
@media (max-width: 800px) {{ body {{ padding: 1rem; overflow-x: auto; }} }}
</style>
</head>
<body>
<h1>{escape(str(session['name']))}</h1>
<div class="meta">
  <span>Session #{session_id}</span>
  <span>{escape(str(session['kind']))} · {escape(str(session['platform']))}</span>
  <span>Status: {escape(str(session['status']))}</span>
  <span>Scheduled: {escape(_fmt_epoch(session['scheduled_at'], timezone))}</span>
  <span>Duration: {session['duration_minutes']} min</span>
  <span>Local rated: {'yes' if session['rated'] else 'no'}</span>
</div>
<div id="timer" class="timer"></div>
{result_card}
{'<button id="show-tags">显示标签</button>' if tags_available else ''}
<table>
<thead><tr><th>#</th><th>Site</th><th>Problem</th><th>Result</th><th>Time</th><th>Wrong</th><th>Difficulty</th><th>Target</th>{'<th class="tags" hidden>Tags</th>' if tags_available else ''}</tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>
<p class="note">Submit on the original judge. Run <code>cp-trainer sync</code> and <code>cp-trainer pull-results {session_id}</code> to import verdicts, or use <code>record</code> manually.</p>
<script>
const start = {start_ms};
const duration = {duration_ms};
const status = {json.dumps(str(session['status']))};
const timer = document.getElementById('timer');
document.getElementById('show-tags')?.addEventListener('click', (event) => {{
  const columns = document.querySelectorAll('.tags');
  const reveal = columns[0].hidden;
  columns.forEach(column => column.hidden = !reveal);
  event.target.textContent = reveal ? '隐藏标签' : '显示标签';
}});
function renderTimer() {{
  if (!start || status === 'scheduled') {{ timer.textContent = 'Not started'; return; }}
  if (status === 'finished') {{ timer.textContent = 'Finished'; return; }}
  const remaining = Math.max(0, start + duration - Date.now());
  const total = Math.floor(remaining / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  timer.textContent = `${{String(h).padStart(2,'0')}}:${{String(m).padStart(2,'0')}}:${{String(s).padStart(2,'0')}}`;
}}
renderTimer(); setInterval(renderTimer, 1000);
</script>
</body>
</html>
"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(html, encoding="utf-8")
    return destination


def export_plan_index(
    db: Database,
    session_ids: list[int],
    destination: Path,
    *,
    timezone: str,
) -> Path:
    rows = []
    for sid in session_ids:
        s = db.get_session(sid)
        rows.append(
            f"<tr><td>{sid}</td><td>{escape(_fmt_epoch(s['scheduled_at'], timezone))}</td>"
            f"<td><a href='session-{sid}.html'>{escape(str(s['name']))}</a></td>"
            f"<td>{escape(str(s['platform']))}</td><td>{s['duration_minutes']} min</td></tr>"
        )
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Weekly CP plan</title>
<style>body{{font-family:system-ui,sans-serif;max-width:950px;margin:2rem auto;padding:0 1rem}}table{{width:100%;border-collapse:collapse}}th,td{{padding:.7rem;border-bottom:1px solid #9996;text-align:left}}a{{color:inherit}}</style>
</head><body><h1>Weekly training plan</h1><table><thead><tr><th>ID</th><th>When</th><th>Session</th><th>Platform</th><th>Duration</th></tr></thead><tbody>{''.join(rows)}</tbody></table></body></html>"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(html, encoding="utf-8")
    return destination
