"""Render a saved result as a readable page without trusting agent HTML."""

import html

from markdown_it import MarkdownIt

MARKDOWN = MarkdownIt("js-default").disable("image")

RESULT_LABELS = {
    "completed": ("已完成", "success"),
    "failed": ("执行失败", "error"),
    "reconnecting": ("正在重连", "warning"),
    "aborted": ("已中断", "warning"),
    "waiting": ("等待处理", "warning"),
    "document": ("原话汇总", ""),
}

STYLE = """
:root {
  color-scheme: light dark;
  --page: #f6f7f9; --panel: #fff; --text: #20252e; --muted: #526071;
  --line: #d5dce5; --subtle: #f1f4f8; --link: #165db9;
  --success: #087648; --error: #b32333; --warning: #925100;
}
@media (prefers-color-scheme: dark) {
  :root {
    --page: #161a21; --panel: #1d232c; --text: #e7edf5; --muted: #aab7c6;
    --line: #415065; --subtle: #252e3c; --link: #8cc0ff;
    --success: #74d7a2; --error: #ffa6ac; --warning: #f4bd7a;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--text); font: 16px/1.65 system-ui, sans-serif; }
main { max-width: 790px; margin: 24px auto; padding: 0 clamp(12px, 4vw, 28px); }
header { display: flex; gap: 8px 14px; flex-wrap: wrap; align-items: baseline; margin: 0 0 12px; }
h1.session { margin: 0; color: var(--muted); font-size: 0.875rem; font-weight: 560; overflow-wrap: anywhere; }
.status { font-size: 0.8125rem; font-weight: 650; }
.success { color: var(--success); } .error { color: var(--error); } .warning { color: var(--warning); }
article { min-width: 0; padding: clamp(14px, 4vw, 28px); background: var(--panel);
  border: 1px solid var(--line); border-radius: 12px; overflow-wrap: anywhere; }
article > :first-child { margin-top: 0; } article > :last-child { margin-bottom: 0; }
article h1, article h2, article h3 { font-size: 1.2rem; line-height: 1.4; }
article h4, article h5, article h6 { font-size: 1rem; }
a { color: var(--link); text-underline-offset: 3px; }
a:focus-visible, pre:focus-visible, .table-scroll:focus-visible { outline: 2px solid var(--link); outline-offset: 3px; }
blockquote { margin-inline: 0; padding-inline-start: 14px; border-inline-start: 3px solid var(--line); color: var(--muted); }
pre, :not(pre) > code { background: var(--subtle); border-radius: 6px; }
pre { overflow-x: auto; padding: 12px 14px; border: 1px solid var(--line); }
code { font: 0.88em/1.6 ui-monospace, Consolas, monospace; }
:not(pre) > code { padding: 0.05em 0.25em; }
.table-scroll { overflow-x: auto; margin: 1em 0; }
table { width: 100%; border-collapse: collapse; font-size: 0.9375rem; }
th, td { border: 1px solid var(--line); text-align: start; padding: 8px 10px; min-width: 5em; }
th { background: var(--subtle); } hr { border: 0; border-top: 1px solid var(--line); }
"""


def table_open(tokens, index, options, env):
    return '<div class="table-scroll" tabindex="0" role="region" aria-label="表格"><table>\n'


def table_close(tokens, index, options, env):
    return "</table></div>\n"


MARKDOWN.renderer.rules.update(table_open=table_open, table_close=table_close)


def result_page(title, kind, text):
    label, tone = RESULT_LABELS.get(kind, ("会话记录", ""))
    title = html.escape(title)
    content = MARKDOWN.render(text)
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title}</title><style>{STYLE}</style></head><body><main>"
        f'<header><h1 class="session">{title}</h1><span class="status {tone}">{label}</span></header>'
        f"<article>{content}</article></main></body></html>"
    )
