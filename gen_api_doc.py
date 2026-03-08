"""
生成火山引擎会议纪要前端接入指南 PDF
"""
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Flowable
import os, glob

# ── 注册中文字体 ──────────────────────────────────────────────────────────────
def _find_font(names):
    search_dirs = [
        "/usr/share/fonts", "/usr/local/share/fonts",
        os.path.expanduser("~/.fonts"),
        "/System/Library/Fonts", "/Library/Fonts",
    ]
    for d in search_dirs:
        for n in names:
            hits = glob.glob(f"{d}/**/{n}", recursive=True)
            if hits:
                return hits[0]
    return None

_REGULAR = _find_font(["NotoSansCJK-Regular.ttc", "NotoSansSC-Regular.ttf",
                        "wqy-microhei.ttc", "DroidSansFallback.ttf"])
_BOLD    = _find_font(["NotoSansCJK-Bold.ttc", "NotoSansSC-Bold.ttf",
                        "wqy-microhei.ttc"])
_MONO    = _find_font(["DejaVuSansMono.ttf", "LiberationMono-Regular.ttf",
                        "UbuntuMono-Regular.ttf", "Courier_New.ttf"])

FONT_BODY = "NotoSC"
FONT_BOLD = "NotoSC-Bold"
FONT_MONO = "Mono"

if _REGULAR:
    pdfmetrics.registerFont(TTFont(FONT_BODY, _REGULAR))
else:
    FONT_BODY = "Helvetica"
if _BOLD:
    pdfmetrics.registerFont(TTFont(FONT_BOLD, _BOLD))
else:
    FONT_BOLD = "Helvetica-Bold"
if _MONO:
    pdfmetrics.registerFont(TTFont(FONT_MONO, _MONO))
else:
    FONT_MONO = "Courier"

# ── 颜色方案 ─────────────────────────────────────────────────────────────────
C_BG_DARK   = colors.HexColor("#0f172a")
C_BG_CODE   = colors.HexColor("#1e293b")
C_BG_TIP    = colors.HexColor("#1c2a1e")
C_BG_WARN   = colors.HexColor("#2a1e00")
C_ACCENT_A  = colors.HexColor("#38bdf8")   # 蓝
C_ACCENT_B  = colors.HexColor("#fb923c")   # 橙
C_ACCENT_C  = colors.HexColor("#a78bfa")   # 紫
C_ACCENT_G  = colors.HexColor("#34d399")   # 绿
C_ACCENT_Y  = colors.HexColor("#fbbf24")   # 黄
C_TEXT      = colors.HexColor("#e2e8f0")
C_TEXT_DIM  = colors.HexColor("#94a3b8")
C_BORDER    = colors.HexColor("#334155")
C_WS        = colors.HexColor("#c084fc")
C_POST      = colors.HexColor("#34d399")
C_GET       = colors.HexColor("#fbbf24")
C_PUT       = colors.HexColor("#60a5fa")
C_DEL       = colors.HexColor("#f87171")
C_SSE       = colors.HexColor("#f472b6")

W, H = A4
MARGIN = 18 * mm

def make_styles():
    s = {}
    def ps(name, **kw):
        base = kw.pop("parent", None)
        defaults = dict(fontName=FONT_BODY, fontSize=10, leading=15,
                        textColor=C_TEXT, spaceAfter=4)
        defaults.update(kw)
        s[name] = ParagraphStyle(name, **defaults)

    ps("title",      fontName=FONT_BOLD, fontSize=22, leading=28,
       textColor=C_ACCENT_A, spaceAfter=4, alignment=TA_CENTER)
    ps("subtitle",   fontSize=11, textColor=C_TEXT_DIM,
       spaceAfter=16, alignment=TA_CENTER)
    ps("h1",         fontName=FONT_BOLD, fontSize=14, leading=20,
       textColor=C_ACCENT_A, spaceBefore=14, spaceAfter=6)
    ps("h2",         fontName=FONT_BOLD, fontSize=11, leading=16,
       textColor=C_TEXT, spaceBefore=10, spaceAfter=4)
    ps("body",       fontSize=9.5, leading=15, textColor=C_TEXT, spaceAfter=4)
    ps("body_dim",   fontSize=9, leading=14, textColor=C_TEXT_DIM, spaceAfter=3)
    ps("code",       fontName=FONT_MONO, fontSize=8.5, leading=13,
       textColor=C_ACCENT_G, spaceAfter=2, leftIndent=4)
    ps("code_key",   fontName=FONT_MONO, fontSize=8.5, leading=13,
       textColor=colors.HexColor("#60a5fa"), spaceAfter=2, leftIndent=4)
    ps("warn",       fontSize=9, leading=14,
       textColor=C_ACCENT_Y, spaceAfter=3, leftIndent=6)
    ps("tip",        fontSize=9, leading=14,
       textColor=C_ACCENT_G, spaceAfter=3, leftIndent=6)
    ps("step_title", fontName=FONT_BOLD, fontSize=10, leading=14,
       textColor=C_TEXT, spaceAfter=2)
    ps("step_body",  fontSize=9, leading=14, textColor=C_TEXT_DIM, spaceAfter=2)
    return s

# ── 自定义 Flowable：带背景色的代码块 ───────────────────────────────────────
class CodeBlock(Flowable):
    def __init__(self, lines, width=None, bg=None, border_color=None):
        super().__init__()
        self.lines = lines
        self._width = width or (W - 2 * MARGIN)
        self.bg = bg or C_BG_CODE
        self.border_color = border_color or C_BORDER
        self._pad = 8
        self._line_h = 13
        self.height = self._line_h * len(lines) + self._pad * 2

    def wrap(self, availWidth, availHeight):
        self.width = min(self._width, availWidth)
        return self.width, self.height

    def draw(self):
        c = self.canv
        # 背景
        c.setFillColor(self.bg)
        c.roundRect(0, 0, self.width, self.height, 4, fill=1, stroke=0)
        # 边框
        c.setStrokeColor(self.border_color)
        c.setLineWidth(0.5)
        c.roundRect(0, 0, self.width, self.height, 4, fill=0, stroke=1)
        # 文字
        c.setFont(FONT_MONO, 8.2)
        y = self.height - self._pad - 10
        for line in self.lines:
            # 简单着色：识别关键词
            if line.strip().startswith("#"):
                c.setFillColor(colors.HexColor("#64748b"))
            elif "type:" in line and "completed" in line:
                c.setFillColor(C_ACCENT_Y)
            elif "type:" in line:
                c.setFillColor(C_ACCENT_G)
            elif line.strip().startswith("←") or line.strip().startswith("→"):
                c.setFillColor(C_ACCENT_A)
            elif "audio_id" in line:
                c.setFillColor(C_ACCENT_Y)
            elif "speaker" in line.lower():
                c.setFillColor(C_ACCENT_C)
            elif line.strip().startswith("{") or line.strip().startswith("}"):
                c.setFillColor(colors.HexColor("#64748b"))
            else:
                c.setFillColor(C_TEXT_DIM)
            c.drawString(self._pad, y, line)
            y -= self._line_h

# ── 方法徽章 helper ──────────────────────────────────────────────────────────
class MethodBadge(Flowable):
    METHOD_COLORS = {
        "WS":   (colors.HexColor("#3b0764"), C_WS),
        "POST": (colors.HexColor("#052e16"), C_POST),
        "GET":  (colors.HexColor("#1c1500"), C_GET),
        "PUT":  (colors.HexColor("#0c1a3a"), C_PUT),
        "DEL":  (colors.HexColor("#2d0000"), C_DEL),
        "SSE":  (colors.HexColor("#2d0020"), C_SSE),
    }
    def __init__(self, method, path, desc="", note=""):
        super().__init__()
        self.method = method
        self.path = path
        self.desc = desc
        self.note = note
        self.height = 36 if not desc else (48 if not note else 60)

    def wrap(self, availWidth, availHeight):
        self.width = availWidth
        return self.width, self.height

    def draw(self):
        c = self.canv
        bg, fg = self.METHOD_COLORS.get(self.method, (C_BG_CODE, C_TEXT))
        # 徽章背景
        bw = 36
        c.setFillColor(bg)
        c.roundRect(0, self.height - 18, bw, 16, 3, fill=1, stroke=0)
        c.setStrokeColor(fg)
        c.setLineWidth(0.5)
        c.roundRect(0, self.height - 18, bw, 16, 3, fill=0, stroke=1)
        # 方法文字
        c.setFillColor(fg)
        c.setFont(FONT_MONO, 7.5)
        c.drawCentredString(bw / 2, self.height - 13, self.method)
        # 路径
        c.setFont(FONT_MONO, 8.5)
        c.setFillColor(C_TEXT)
        c.drawString(bw + 6, self.height - 13, self.path)
        # 描述
        if self.desc:
            c.setFont(FONT_BODY, 8.5)
            c.setFillColor(C_TEXT_DIM)
            c.drawString(4, self.height - 30, self.desc)
        if self.note:
            c.setFont(FONT_BODY, 8)
            c.setFillColor(C_ACCENT_Y)
            c.drawString(4, self.height - 42, "⚠ " + self.note)

# ── 分割线 ───────────────────────────────────────────────────────────────────
def hr(color=C_BORDER):
    return HRFlowable(width="100%", thickness=0.5, color=color, spaceAfter=8, spaceBefore=4)

# ── Section 标题带色条 ────────────────────────────────────────────────────────
class SectionHeader(Flowable):
    def __init__(self, label, badge, badge_color, title):
        super().__init__()
        self.label = label
        self.badge = badge
        self.badge_color = badge_color
        self.title = title
        self.height = 28

    def wrap(self, aw, ah):
        self.width = aw
        return self.width, self.height

    def draw(self):
        c = self.canv
        # 左侧色条
        c.setFillColor(self.badge_color)
        c.rect(0, 0, 4, self.height, fill=1, stroke=0)
        # 徽章
        bw = 56
        c.setFillColor(self.badge_color)
        alpha_bg = colors.HexColor(self.badge_color.hexval())
        c.setFillColorRGB(*[x * 0.25 for x in self.badge_color.rgb()])
        c.roundRect(10, 6, bw, 16, 3, fill=1, stroke=0)
        c.setStrokeColor(self.badge_color)
        c.setLineWidth(0.5)
        c.roundRect(10, 6, bw, 16, 3, fill=0, stroke=1)
        c.setFillColor(self.badge_color)
        c.setFont(FONT_BOLD, 8)
        c.drawCentredString(10 + bw / 2, 11, self.badge)
        # 标题
        c.setFillColor(C_TEXT)
        c.setFont(FONT_BOLD, 12)
        c.drawString(10 + bw + 10, 10, self.title)

# ── 说话人示例表 ─────────────────────────────────────────────────────────────
def speaker_example_table(styles):
    rows = [
        ["说话人", "文字内容", "start_ms", "end_ms"],
        ["说话人1", "今天讨论Q2预算分配方案，请各部门汇报", "0", "3500"],
        ["说话人2", "我们部门需要额外申请20万用于服务器扩容", "3800", "7200"],
        ["说话人3", "市场部这边需要30万做品牌推广", "7500", "10100"],
        ["说话人1", "好的，我们来整理一下这些需求...", "10400", "13000"],
    ]
    col_w = [(W - 2 * MARGIN) * x for x in [0.14, 0.54, 0.16, 0.16]]
    speaker_colors = [C_ACCENT_A, C_ACCENT_B, C_ACCENT_G]

    ts = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), C_BG_CODE),
        ("TEXTCOLOR",  (0, 0), (-1, 0), C_TEXT_DIM),
        ("FONTNAME",   (0, 0), (-1, 0), FONT_BOLD),
        ("FONTSIZE",   (0, 0), (-1, -1), 8.5),
        ("FONTNAME",   (0, 1), (-1, -1), FONT_BODY),
        ("FONTNAME",   (2, 1), (-1, -1), FONT_MONO),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [C_BG_CODE, colors.HexColor("#253045")]),
        ("GRID",       (0, 0), (-1, -1), 0.4, C_BORDER),
        ("ROUNDEDCORNERS", [4]),
        ("TEXTCOLOR",  (2, 1), (-1, -1), C_TEXT_DIM),
    ])
    for i, sc in enumerate(speaker_colors):
        ts.add("TEXTCOLOR", (0, i + 1), (0, i + 1), sc)
        ts.add("FONTNAME",  (0, i + 1), (0, i + 1), FONT_BOLD)

    table_data = []
    for r in rows:
        table_data.append([Paragraph(cell, styles["body_dim"]) if j > 0 else
                           Paragraph(f"<b>{cell}</b>", styles["body"]) for j, cell in enumerate(r)])

    return Table(table_data, colWidths=col_w, style=ts)

# ── CRUD 接口表 ───────────────────────────────────────────────────────────────
def crud_table(styles):
    rows = [
        ["方法", "路径", "说明"],
        ["PUT",    "/volc/{id}/transcript",       "修改转写文本"],
        ["PUT",    "/volc/{id}/summary",           "修改摘要内容"],
        ["POST",   "/volc/{id}/todos",             "新增待办事项"],
        ["PUT",    "/volc/{id}/todos/{todo_id}",   "修改待办事项"],
        ["DELETE", "/volc/{id}/todos/{todo_id}",   "删除待办事项"],
    ]
    method_c = {"PUT": C_PUT, "POST": C_POST, "DELETE": C_DEL}
    col_w = [(W - 2 * MARGIN) * x for x in [0.12, 0.52, 0.36]]
    ts = TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), C_BG_CODE),
        ("TEXTCOLOR",     (0, 0), (-1, 0), C_TEXT_DIM),
        ("FONTNAME",      (0, 0), (-1, 0), FONT_BOLD),
        ("FONTSIZE",      (0, 0), (-1, -1), 8.5),
        ("FONTNAME",      (0, 1), (-1, -1), FONT_BODY),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [C_BG_CODE, colors.HexColor("#253045")]),
        ("GRID",          (0, 0), (-1, -1), 0.4, C_BORDER),
        ("FONTNAME",      (1, 1), (1, -1), FONT_MONO),
        ("TEXTCOLOR",     (1, 1), (1, -1), C_TEXT_DIM),
    ])
    for i, row in enumerate(rows[1:], 1):
        c = method_c.get(row[0], C_TEXT)
        ts.add("TEXTCOLOR", (0, i), (0, i), c)
        ts.add("FONTNAME",  (0, i), (0, i), FONT_BOLD)

    data = [[Paragraph(f"<b>{c}</b>" if j == 0 else c, styles["body"])
             for j, c in enumerate(r)] for r in rows]
    return Table(data, colWidths=col_w, style=ts)

# ── 构建文档 ─────────────────────────────────────────────────────────────────
def build(output="volc_meeting_api_guide.pdf"):
    doc = SimpleDocTemplate(
        output, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
        title="火山引擎会议纪要前端接入指南",
        author="后端团队",
    )

    styles = make_styles()
    story = []

    # ── 封面区 ──
    story.append(Spacer(1, 10 * mm))
    story.append(Paragraph("火山引擎会议纪要", styles["title"]))
    story.append(Paragraph("前端接入指南 · meeting_minute_volc.py", styles["subtitle"]))
    story.append(hr(C_ACCENT_A))
    story.append(Spacer(1, 4 * mm))

    # ── 概览 ──
    story.append(Paragraph("📌 概览", styles["h1"]))
    story.append(Paragraph(
        "本模块提供两条完整工作流，前端按顺序调用各按钮即可完成录音→转写→生成会议纪要的全流程。"
        "两条工作流的最后一步（提交妙记）复用同一接口。",
        styles["body"]
    ))
    story.append(Spacer(1, 3 * mm))

    overview_data = [
        ["工作流", "适用场景", "步骤数"],
        ["工作流 A — 实时录音", "前端麦克风直接录音，实时出字", "2步"],
        ["工作流 B — 上传文件", "已有录音文件（WAV/MP3/M4A/OGG）", "3步"],
    ]
    ov_col = [(W - 2*MARGIN)*x for x in [0.28, 0.52, 0.20]]
    ov_ts = TableStyle([
        ("BACKGROUND", (0,0), (-1,0), C_BG_CODE),
        ("TEXTCOLOR",  (0,0), (-1,0), C_TEXT_DIM),
        ("FONTNAME",   (0,0), (-1,0), FONT_BOLD),
        ("FONTNAME",   (0,1), (-1,-1), FONT_BODY),
        ("FONTSIZE",   (0,0), (-1,-1), 9),
        ("TOPPADDING", (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
        ("LEFTPADDING",   (0,0), (-1,-1), 10),
        ("GRID", (0,0), (-1,-1), 0.4, C_BORDER),
        ("TEXTCOLOR", (0,1), (0,1), C_ACCENT_A),
        ("TEXTCOLOR", (0,2), (0,2), C_ACCENT_B),
        ("FONTNAME",  (0,1), (0,-1), FONT_BOLD),
        ("TEXTCOLOR", (2,1), (2,-1), C_ACCENT_G),
        ("ALIGN",     (2,0), (2,-1), "CENTER"),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [C_BG_CODE, colors.HexColor("#253045")]),
    ])
    ov_para = [[Paragraph(c, styles["body"]) for c in r] for r in overview_data]
    story.append(Table(ov_para, colWidths=ov_col, style=ov_ts))
    story.append(Spacer(1, 5 * mm))

    # ══════════════════════════════════════════════════════════
    # 工作流 A
    # ══════════════════════════════════════════════════════════
    story.append(SectionHeader("A", "工作流 A", C_ACCENT_A, "实时录音模式"))
    story.append(Spacer(1, 3 * mm))

    # Step A1
    story.append(KeepTogether([
        Paragraph("步骤 1 — 建立 WebSocket 实时录音", styles["h2"]),
        MethodBadge("WS", "/api/minutes/volc/{meeting_id}/live?token=JWT",
                    "前端持续发送 PCM 音频帧，服务端实时推送识别文字"),
        Spacer(1, 2*mm),
        Paragraph("前端发送（二进制）：PCM 音频帧，16kHz / 16-bit / 单声道", styles["body_dim"]),
        Paragraph("前端发送（JSON）：控制指令", styles["body_dim"]),
        CodeBlock([
            '{"action": "stop"}                        // 结束录音',
            '{"action": "config", "rate": 16000, "channels": 1}  // 可选配置',
        ]),
        Spacer(1, 2*mm),
        Paragraph("服务端推送事件：", styles["body_dim"]),
        CodeBlock([
            '← {type:"session_created", session_id: 123}',
            '← {type:"partial",   text:"...", accumulated:"..."}   // 实时出字',
            '← {type:"final",     text:"...", accumulated:"..."}',
            '← {type:"completed", session_id:123, audio_id:456,    // ★ 存下 audio_id！',
            '     transcript:"...", audio_uploaded:true,',
            '     duration_seconds: 60.0}',
            '← {type:"error",     message:"..."}',
        ]),
        Paragraph(
            "⚠ 收到 completed 后务必将 audio_id 存入状态，用于步骤2触发提交。",
            styles["warn"]
        ),
    ]))
    story.append(Spacer(1, 3 * mm))

    # Step A2
    story.append(KeepTogether([
        Paragraph("步骤 2 — 提交豆包语音妙记（精准转写 + 说话人 + 摘要 + Todos）", styles["h2"]),
        MethodBadge("POST", "/api/minutes/volc/{meeting_id}/submit",
                    "无需请求体，服务端自动取最新一条 TOS 音频记录提交"),
        Spacer(1, 2*mm),
        Paragraph("返回值（立即）：", styles["body_dim"]),
        CodeBlock([
            '← {success:true, data:{status:"submitted", task_id:"xxx"}}',
        ]),
        Paragraph("后台完成后，通过 Meeting WebSocket 推送：", styles["body_dim"]),
        CodeBlock([
            '← {type:"volc_minutes_status",    status:"processing"}  // 处理中',
            '← {type:"volc_minutes_completed", refresh:true}         // ★ 完成，去查询！',
            '← {type:"volc_minutes_failed",    error:"..."}          // 失败',
        ]),
        Paragraph(
            "✔ 收到 volc_minutes_completed 后立刻调用 GET /api/minutes/volc/{meeting_id} 获取最终结果。",
            styles["tip"]
        ),
    ]))
    story.append(Spacer(1, 5 * mm))

    # ══════════════════════════════════════════════════════════
    # 工作流 B
    # ══════════════════════════════════════════════════════════
    story.append(SectionHeader("B", "工作流 B", C_ACCENT_B, "上传文件模式"))
    story.append(Spacer(1, 3 * mm))

    # Step B1
    story.append(KeepTogether([
        Paragraph("步骤 1 — 上传音频文件到对象存储", styles["h2"]),
        MethodBadge("POST", "/api/minutes/volc/{meeting_id}/upload",
                    "Content-Type: multipart/form-data，字段名: file"),
        Spacer(1, 2*mm),
        Paragraph("支持格式：WAV / MP3 / M4A / OGG 等", styles["body_dim"]),
        CodeBlock([
            '← {success:true, data:{id: audio_id, status:"uploaded"}}  // ★ 存下 audio_id！',
        ]),
    ]))
    story.append(Spacer(1, 3 * mm))

    # Step B2
    story.append(KeepTogether([
        Paragraph("步骤 2 — SSE 流式 ASR 转写（实时出字）", styles["h2"]),
        MethodBadge("SSE", "/api/minutes/volc/audio/{audio_id}/stream?token=JWT",
                    "使用 EventSource 连接，不需要 POST body"),
        Spacer(1, 2*mm),
        CodeBlock([
            "const es = new EventSource(",
            "  `/api/minutes/volc/audio/${audioId}/stream?token=${token}`",
            ");",
            "es.onmessage = (e) => {",
            '  const msg = JSON.parse(e.data);',
            '  if (msg.type === "partial")   updateDisplay(msg.accumulated);',
            '  if (msg.type === "completed") { es.close(); triggerSubmit(); }',
            "};",
        ]),
        Spacer(1, 2*mm),
        Paragraph("服务端推送事件：", styles["body_dim"]),
        CodeBlock([
            '← data:{type:"session_created", session_id:123, audio_id:456}',
            '← data:{type:"partial",   text:"...", accumulated:"..."}',
            '← data:{type:"final",     text:"...", accumulated:"..."}',
            '← data:{type:"completed", session_id:123, transcript:"..."}  // ★ 转写落库',
            '← data:{type:"error",     message:"..."}',
        ]),
    ]))
    story.append(Spacer(1, 3 * mm))

    # Step B3
    story.append(KeepTogether([
        Paragraph("步骤 3 — 提交豆包语音妙记（与工作流A步骤2完全相同）", styles["h2"]),
        MethodBadge("POST", "/api/minutes/volc/{meeting_id}/submit",
                    "复用同一接口，流程完全相同"),
    ]))
    story.append(Spacer(1, 5 * mm))

    # ══════════════════════════════════════════════════════════
    # 查询结果
    # ══════════════════════════════════════════════════════════
    story.append(SectionHeader("C", "共享", C_ACCENT_C, "查询最终会议纪要结果"))
    story.append(Spacer(1, 3 * mm))
    story.append(MethodBadge("GET", "/api/minutes/volc/{meeting_id}",
                              "在收到 volc_minutes_completed 推送后调用"))
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph("返回结构：", styles["body_dim"]))
    story.append(CodeBlock([
        "{",
        '  "transcript_text":   "[说话人1] 今天讨论预算...\\n[说话人2] 我认为...",',
        '  "speaker_segments": [                       // ← 说话人分段（重点！）',
        '    {"speaker":"说话人1","text":"...","start_ms":0,"end_ms":3500},',
        '    {"speaker":"说话人2","text":"...","start_ms":3800,"end_ms":7200}',
        "  ],",
        '  "summary": {"title":"Q2预算会议","paragraph":"会议讨论了..."},',
        '  "todos":   [{"id":1,"content":"整理预算","assignee":"张三","deadline":"..."}]',
        "}",
    ]))
    story.append(Spacer(1, 5 * mm))

    # ══════════════════════════════════════════════════════════
    # 说话人区分（重点章节）
    # ══════════════════════════════════════════════════════════
    story.append(SectionHeader("D", "重点", C_ACCENT_Y, "说话人区分（Speaker Diarization）"))
    story.append(Spacer(1, 3 * mm))

    story.append(Paragraph(
        "说话人区分由豆包语音妙记完成，仅在 /submit 处理完成后可用。"
        "流式 ASR 阶段无说话人信息，只有裸文字。",
        styles["body"]
    ))
    story.append(Spacer(1, 2 * mm))

    # 阶段对比表
    phase_data = [
        ["阶段", "说话人信息", "数据来源", "前端用途"],
        ["ASR 流式（partial/final）", "❌ 无", "volc ASR WebSocket", "进度条 / 实时展示"],
        ["妙记完成后（GET 查询）",    "✅ 有", "豆包语音妙记",       "最终展示 / 对话气泡"],
    ]
    ph_col = [(W - 2*MARGIN)*x for x in [0.30, 0.14, 0.28, 0.28]]
    ph_ts = TableStyle([
        ("BACKGROUND", (0,0), (-1,0), C_BG_CODE),
        ("TEXTCOLOR",  (0,0), (-1,0), C_TEXT_DIM),
        ("FONTNAME",   (0,0), (-1,0), FONT_BOLD),
        ("FONTNAME",   (0,1), (-1,-1), FONT_BODY),
        ("FONTSIZE",   (0,0), (-1,-1), 9),
        ("TOPPADDING", (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
        ("LEFTPADDING",   (0,0), (-1,-1), 8),
        ("GRID", (0,0), (-1,-1), 0.4, C_BORDER),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [C_BG_CODE, colors.HexColor("#253045")]),
        ("TEXTCOLOR", (1,1), (1,1), C_DEL),
        ("TEXTCOLOR", (1,2), (1,2), C_ACCENT_G),
    ])
    ph_para = [[Paragraph(c, styles["body"]) for c in r] for r in phase_data]
    story.append(Table(ph_para, colWidths=ph_col, style=ph_ts))
    story.append(Spacer(1, 3 * mm))

    # 说话人示例数据
    story.append(Paragraph("speaker_segments 数据示例：", styles["h2"]))
    story.append(speaker_example_table(styles))
    story.append(Spacer(1, 3 * mm))

    # 前端渲染建议
    story.append(Paragraph("前端渲染建议（伪代码）：", styles["h2"]))
    story.append(CodeBlock([
        "// 收到 volc_minutes_completed 后：",
        "const result = await GET(`/api/minutes/volc/${meetingId}`);",
        "",
        "if (result.speaker_segments.length > 0) {",
        "  // 优先：渲染带颜色标签的对话气泡",
        "  // speaker 取值：'说话人1' / '说话人2' / ...",
        "  // 可用 start_ms / end_ms 做音频时间轴高亮",
        "  renderSpeakerBubbles(result.speaker_segments);",
        "} else {",
        "  // 降级：渲染纯文本（含 [说话人N] 前缀，若单人则无前缀）",
        "  renderPlainText(result.transcript_text);",
        "}",
    ]))
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph(
        "⚠ speaker_segments 为空列表说明：音频人声无法区分（单人、重叠严重等），"
        "此时 transcript_text 仍然有效，直接渲染即可。",
        styles["warn"]
    ))
    story.append(Spacer(1, 5 * mm))

    # ══════════════════════════════════════════════════════════
    # 编辑接口
    # ══════════════════════════════════════════════════════════
    story.append(SectionHeader("E", "辅助", C_TEXT_DIM, "查询 & 编辑接口（妙记完成后可用）"))
    story.append(Spacer(1, 3 * mm))
    story.append(crud_table(styles))
    story.append(Spacer(1, 5 * mm))

    # ══════════════════════════════════════════════════════════
    # 接口速查表
    # ══════════════════════════════════════════════════════════
    story.append(SectionHeader("F", "速查", C_ACCENT_G, "完整接口速查表"))
    story.append(Spacer(1, 3 * mm))

    all_apis = [
        ["方法", "路径", "认证", "说明"],
        ["WS",   "/api/minutes/volc/{id}/live?token=JWT",          "token(Query)", "工作流A步骤1：实时录音"],
        ["POST", "/api/minutes/volc/{id}/upload",                   "Bearer JWT",   "工作流B步骤1：上传文件"],
        ["GET",  "/api/minutes/volc/audio/{audio_id}/stream?token", "token(Query)", "工作流B步骤2：SSE转写"],
        ["POST", "/api/minutes/volc/{id}/submit",                   "Bearer JWT",   "共用步骤：提交妙记"],
        ["GET",  "/api/minutes/volc/{id}",                          "Bearer JWT",   "查询最终纪要结果"],
        ["PUT",  "/api/minutes/volc/{id}/transcript",               "Bearer JWT",   "修改转写文本"],
        ["PUT",  "/api/minutes/volc/{id}/summary",                  "Bearer JWT",   "修改摘要"],
        ["POST", "/api/minutes/volc/{id}/todos",                    "Bearer JWT",   "新增Todo"],
        ["PUT",  "/api/minutes/volc/{id}/todos/{todo_id}",          "Bearer JWT",   "修改Todo"],
        ["DEL",  "/api/minutes/volc/{id}/todos/{todo_id}",          "Bearer JWT",   "删除Todo"],
    ]
    api_col = [(W - 2*MARGIN)*x for x in [0.08, 0.44, 0.18, 0.30]]
    method_c2 = {"WS": C_WS, "POST": C_POST, "GET": C_GET, "PUT": C_PUT, "DEL": C_DEL, "SSE": C_SSE}
    api_ts = TableStyle([
        ("BACKGROUND", (0,0), (-1,0), C_BG_CODE),
        ("TEXTCOLOR",  (0,0), (-1,0), C_TEXT_DIM),
        ("FONTNAME",   (0,0), (-1,0), FONT_BOLD),
        ("FONTNAME",   (0,1), (-1,-1), FONT_BODY),
        ("FONTNAME",   (1,1), (1,-1), FONT_MONO),
        ("FONTNAME",   (2,1), (2,-1), FONT_MONO),
        ("FONTSIZE",   (0,0), (-1,-1), 8),
        ("TOPPADDING", (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ("LEFTPADDING",   (0,0), (-1,-1), 6),
        ("GRID", (0,0), (-1,-1), 0.4, C_BORDER),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [C_BG_CODE, colors.HexColor("#253045")]),
        ("TEXTCOLOR", (1,1), (1,-1), C_TEXT_DIM),
        ("TEXTCOLOR", (2,1), (2,-1), colors.HexColor("#64748b")),
        ("ALIGN", (0,0), (0,-1), "CENTER"),
    ])
    for i, row in enumerate(all_apis[1:], 1):
        c = method_c2.get(row[0], C_TEXT)
        api_ts.add("TEXTCOLOR", (0, i), (0, i), c)
        api_ts.add("FONTNAME",  (0, i), (0, i), FONT_BOLD)

    api_para = [[Paragraph(c, styles["body"]) for c in r] for r in all_apis]
    story.append(Table(api_para, colWidths=api_col, style=api_ts))
    story.append(Spacer(1, 3 * mm))

    # ── 页脚提示 ──
    story.append(hr())
    story.append(Paragraph(
        "注：所有 Bearer JWT 接口需在 HTTP Header 中携带 Authorization: Bearer &lt;token&gt;。"
        "Meeting WebSocket 消息通过独立的会议 WS 通道推送（非本文档中的录音 WS）。",
        styles["body_dim"]
    ))

    doc.build(story)
    print(f"PDF 已生成：{output}")

if __name__ == "__main__":
    build("/root/workspace/rag/gongwen-rag-system/volc_meeting_api_guide.pdf")
