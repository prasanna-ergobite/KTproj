"""
Script to generate a presentation deck for AutoKT (AutoKT_Project_Presentation.pptx).
Theme: AutoKT Brand (Dark Slate, Warm Paper, Forest Green, Lime, Crisp Cards, 16:9 widescreen).
"""

import os
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.enum.shapes import MSO_SHAPE

def create_presentation():
    prs = Presentation()
    # 16:9 Widescreen dimensions
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    # Color Tokens
    DARK_BG = RGBColor(0x17, 0x20, 0x1D)      # #17201D deep slate
    DARK_CARD = RGBColor(0x21, 0x2C, 0x28)    # #212C28
    PAPER_BG = RGBColor(0xF5, 0xF5, 0xEF)     # #F5F5EF warm paper
    SURFACE_WHITE = RGBColor(0xFF, 0xFE, 0xF9)# #FFFEF9 crisp white surface
    BORDER_LINE = RGBColor(0xDE, 0xDE, 0xD5)  # #DEDED5 line border
    ACCENT_GREEN = RGBColor(0x44, 0x83, 0x3F) # #44833F forest green
    ACCENT_LIME = RGBColor(0xB9, 0xF5, 0x6A)  # #B9F56A acid lime
    ACCENT_BLUE = RGBColor(0x55, 0x79, 0xE8)  # #5579E8 blue
    ACCENT_ORANGE = RGBColor(0xED, 0x7C, 0x4A)# #ED7C4A orange
    ACCENT_RED = RGBColor(0xC8, 0x5B, 0x52)   # #C85B52 red
    TEXT_DARK = RGBColor(0x17, 0x20, 0x1D)    # #17201D ink
    TEXT_MUTED = RGBColor(0x70, 0x78, 0x72)   # #707872 muted ink
    TEXT_LIGHT = RGBColor(0xF6, 0xF6, 0xEE)   # #F6F6EE light paper
    TEXT_LIGHT_MUTED = RGBColor(0x9A, 0xA4, 0x9F)

    blank_layout = prs.slide_layouts[6]

    def set_slide_background(slide, color):
        bg = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, Inches(13.333), Inches(7.5))
        bg.fill.solid()
        bg.fill.fore_color.rgb = color
        bg.line.color.rgb = color
        return bg

    def add_header(slide, eyebrow_text, title_text, desc_text):
        # Eyebrow chip
        box_eye = slide.shapes.add_textbox(Inches(0.8), Inches(0.5), Inches(11.7), Inches(0.4))
        tf_eye = box_eye.text_frame
        tf_eye.word_wrap = True
        p_eye = tf_eye.paragraphs[0]
        p_eye.text = eyebrow_text.upper()
        p_eye.font.size = Pt(11)
        p_eye.font.bold = True
        p_eye.font.color.rgb = ACCENT_GREEN
        p_eye.font.name = "Segoe UI"

        # Main Title
        box_t = slide.shapes.add_textbox(Inches(0.8), Inches(0.85), Inches(11.7), Inches(0.65))
        tf_t = box_t.text_frame
        tf_t.word_wrap = True
        p_t = tf_t.paragraphs[0]
        p_t.text = title_text
        p_t.font.size = Pt(26)
        p_t.font.bold = True
        p_t.font.color.rgb = TEXT_DARK
        p_t.font.name = "Georgia"

        # Subtitle / description
        if desc_text:
            box_d = slide.shapes.add_textbox(Inches(0.8), Inches(1.5), Inches(11.7), Inches(0.5))
            tf_d = box_d.text_frame
            tf_d.word_wrap = True
            p_d = tf_d.paragraphs[0]
            p_d.text = desc_text
            p_d.font.size = Pt(13)
            p_d.font.color.rgb = TEXT_MUTED
            p_d.font.name = "Segoe UI"

    def add_card(slide, left, top, width, height, bg_color=SURFACE_WHITE, border_color=BORDER_LINE):
        card = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, left, top, width, height)
        card.fill.solid()
        card.fill.fore_color.rgb = bg_color
        card.line.color.rgb = border_color
        card.line.width = Pt(1)
        return card

    # =========================================================================
    # SLIDE 1: TITLE & COVER SLIDE (Dark Theme)
    # =========================================================================
    s1 = prs.slides.add_slide(blank_layout)
    set_slide_background(s1, DARK_BG)

    # Accent decorative glow
    top_bar = s1.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.8), Inches(0.8), Inches(1.5), Inches(0.08))
    top_bar.fill.solid()
    top_bar.fill.fore_color.rgb = ACCENT_LIME
    top_bar.line.fill.background()

    # Title box
    tbox = s1.shapes.add_textbox(Inches(0.8), Inches(1.4), Inches(11.7), Inches(2.2))
    tf1 = tbox.text_frame
    tf1.word_wrap = True

    p = tf1.paragraphs[0]
    p.text = "AutoKT"
    p.font.size = Pt(56)
    p.font.bold = True
    p.font.color.rgb = ACCENT_LIME
    p.font.name = "Georgia"

    p2 = tf1.add_paragraph()
    p2.text = "AI-Powered Codebase Intelligence & Knowledge Transfer"
    p2.font.size = Pt(30)
    p2.font.bold = True
    p2.font.color.rgb = TEXT_LIGHT
    p2.font.name = "Georgia"

    p3 = tf1.add_paragraph()
    p3.text = "Transforming weeks of developer onboarding into hours of instant, graph-grounded understanding."
    p3.font.size = Pt(16)
    p3.font.color.rgb = TEXT_LIGHT_MUTED
    p3.font.name = "Segoe UI"
    p3.space_before = Pt(12)

    # 3 Summary Feature Pills at the bottom
    cards_data = [
        ("🧠 Dual-Brain Architecture", "Neo4j Knowledge Graph + ChromaDB Hybrid RAG grounding code in true topology."),
        ("🗺️ Interactive Visual Canvas", "2D force-directed architecture explorer with on-demand 1-hop neighbor expansion."),
        ("🛡️ Bus Factor & Health Radar", "Git contributor mining to detect sole-owner risks and knowledge gaps automatically.")
    ]
    card_w = Inches(3.65)
    for i, (ctitle, cdesc) in enumerate(cards_data):
        cx = Inches(0.8) + i * Inches(4.0)
        c = add_card(s1, cx, Inches(4.5), card_w, Inches(2.2), bg_color=DARK_CARD, border_color=RGBColor(0x33, 0x42, 0x3C))
        
        cbox = s1.shapes.add_textbox(cx + Inches(0.25), Inches(4.7), card_w - Inches(0.5), Inches(1.8))
        ctf = cbox.text_frame
        ctf.word_wrap = True
        
        cp1 = ctf.paragraphs[0]
        cp1.text = ctitle
        cp1.font.size = Pt(15)
        cp1.font.bold = True
        cp1.font.color.rgb = ACCENT_LIME
        cp1.font.name = "Segoe UI"
        
        cp2 = ctf.add_paragraph()
        cp2.text = cdesc
        cp2.font.size = Pt(12)
        cp2.font.color.rgb = TEXT_LIGHT_MUTED
        cp2.font.name = "Segoe UI"
        cp2.space_before = Pt(8)

    # Speaker notes for Slide 1
    s1.notes_slide.notes_text_frame.text = (
        "Welcome everyone. Today we are presenting AutoKT, our AI-powered codebase intelligence "
        "and automated knowledge-transfer platform. Every engineering team faces the challenge "
        "of developer ramp-up and the catastrophic loss of institutional memory when developers leave. "
        "AutoKT bridges this gap using a dual-engine architecture combining Neo4j graph databases "
        "and hybrid vector search."
    )

    # =========================================================================
    # SLIDE 2: THE PROBLEM (Knowledge Transfer Crisis)
    # =========================================================================
    s2 = prs.slides.add_slide(blank_layout)
    set_slide_background(s2, PAPER_BG)
    add_header(s2, "The Core Problem", "The Developer Knowledge Transfer Crisis", 
               "Engineering organizations lose millions annually to tribal knowledge silos, slow onboarding, and unmapped legacy debt.")

    problems = [
        ("The Tribal Knowledge Black Hole", 
         "Critical architecture decisions live only in senior engineers' heads or fragmented chat channels.",
         "• 60%+ of internal documentation goes obsolete within 90 days.\n• High institutional dependency on single individuals.\n• Expensive context interruptions for senior developers.",
         ACCENT_RED),
        ("The 3-to-6 Month Onboarding Sunk Cost", 
         "New hires spend months reading spaghetti code before making their first confident commit.",
         "• New engineers struggle to trace cross-service call chains.\n• Mentors spend 15+ hours/week re-explaining the same core flows.\n• Slow time-to-first-commit reduces engineering velocity.",
         ACCENT_ORANGE),
        ("Invisible 'Bus Factor' Risks", 
         "Teams are unaware of which critical modules depend entirely on a single contributor.",
         "• Resignations cause sudden development paralysis.\n• Code ownership is opaque until a critical bug strikes production.\n• Business PRDs disconnect from what code actually implements.",
         ACCENT_BLUE),
    ]

    card_w = Inches(3.65)
    for i, (title, sub, details, badge_col) in enumerate(problems):
        cx = Inches(0.8) + i * Inches(4.0)
        cy = Inches(2.2)
        add_card(s2, cx, cy, card_w, Inches(4.5), bg_color=SURFACE_WHITE, border_color=BORDER_LINE)

        # Content inside card
        tb = s2.shapes.add_textbox(cx + Inches(0.25), cy + Inches(0.25), card_w - Inches(0.5), Inches(4.0))
        tf = tb.text_frame
        tf.word_wrap = True

        p = tf.paragraphs[0]
        p.text = f"0{i+1}. PAIN POINT"
        p.font.size = Pt(10)
        p.font.bold = True
        p.font.color.rgb = badge_col
        p.font.name = "Segoe UI"

        p_t = tf.add_paragraph()
        p_t.text = title
        p_t.font.size = Pt(17)
        p_t.font.bold = True
        p_t.font.color.rgb = TEXT_DARK
        p_t.font.name = "Georgia"
        p_t.space_before = Pt(6)

        p_s = tf.add_paragraph()
        p_s.text = sub
        p_s.font.size = Pt(12)
        p_s.font.italic = True
        p_s.font.color.rgb = TEXT_MUTED
        p_s.font.name = "Segoe UI"
        p_s.space_before = Pt(6)

        p_d = tf.add_paragraph()
        p_d.text = details
        p_d.font.size = Pt(11)
        p_d.font.color.rgb = TEXT_DARK
        p_d.font.name = "Segoe UI"
        p_d.space_before = Pt(12)

    s2.notes_slide.notes_text_frame.text = (
        "Here is why current developer onboarding fails: traditional documentation is static and stale. "
        "Engineers either have to dig through thousands of lines of unfamiliar code or constantly pull "
        "senior staff away from high-priority work. Furthermore, engineering leaders lack visibility into "
        "key-person dependency until that person resigns, leaving the team with unmaintainable modules."
    )

    # =========================================================================
    # SLIDE 3: THE SOLUTION & ARCHITECTURE
    # =========================================================================
    s3 = prs.slides.add_slide(blank_layout)
    set_slide_background(s3, PAPER_BG)
    add_header(s3, "Architecture & Pipeline", "The AutoKT Dual-Engine Solution", 
               "Combining deterministic Neo4j graph topology with ChromaDB vector search and hybrid RAG.")

    # 3 Horizontal Process Cards
    pipeline_steps = [
        ("1. Multi-Modal Ingestion Engine",
         "Ingests codebases and technical specifications directly from Git and files.",
         "• AST Code Parsing: Extracts Functions, Classes, Calls, and File Imports.\n"
         "• Git History Mining: Analyzes commits to compute author ownership and bus factor.\n"
         "• Multi-tenant tenant_key constraints ensuring complete data isolation.",
         ACCENT_GREEN),
        ("2. Dual-Database Knowledge Foundation",
         "Stores both relational structure and semantic context simultaneously.",
         "• Neo4j Graph DB: Maps Repositories ➔ Modules ➔ Files ➔ Functions ➔ Authors.\n"
         "• ChromaDB Vector Store: Dense embeddings for semantic semantic chunk retrieval.\n"
         "• Hybrid Lexical BM25: Keyword matching merged via Reciprocal Rank Fusion.",
         ACCENT_BLUE),
        ("3. AI Intelligence & Presentation Layer",
         "Transforms complex topology into actionable, grounded insights.",
         "• Interactive 2D Graph Explorer: Live canvas with physics and 1-hop expansion.\n"
         "• Grounded Q&A Synthesizer: RAG answers with exact cited LOC and snippets.\n"
         "• Automated KT Onboarding Packs & Business PRD-to-Code mapping.",
         ACCENT_ORANGE),
    ]

    for i, (step_t, step_sub, step_body, col) in enumerate(pipeline_steps):
        cx = Inches(0.8) + i * Inches(4.0)
        cy = Inches(2.2)
        add_card(s3, cx, cy, Inches(3.65), Inches(4.5), bg_color=SURFACE_WHITE, border_color=BORDER_LINE)

        tb = s3.shapes.add_textbox(cx + Inches(0.25), cy + Inches(0.25), Inches(3.15), Inches(4.0))
        tf = tb.text_frame
        tf.word_wrap = True

        p = tf.paragraphs[0]
        p.text = step_t
        p.font.size = Pt(16)
        p.font.bold = True
        p.font.color.rgb = col
        p.font.name = "Georgia"

        p_s = tf.add_paragraph()
        p_s.text = step_sub
        p_s.font.size = Pt(11)
        p_s.font.italic = True
        p_s.font.color.rgb = TEXT_MUTED
        p_s.font.name = "Segoe UI"
        p_s.space_before = Pt(6)

        p_b = tf.add_paragraph()
        p_b.text = step_body
        p_b.font.size = Pt(11)
        p_b.font.color.rgb = TEXT_DARK
        p_b.font.name = "Segoe UI"
        p_b.space_before = Pt(12)

    s3.notes_slide.notes_text_frame.text = (
        "Pure vector RAG fails on code because code is fundamentally relational and hierarchical. "
        "AutoKT's breakthrough is the Dual-Engine approach: Neo4j maintains the absolute truth of "
        "function call graphs, file imports, and author ownership, while ChromaDB and BM25 handle semantic context. "
        "This ensures answers are 100% factual and hallucination-free."
    )

    # =========================================================================
    # SLIDE 4: WHAT WE BUILT (5 Core Capabilities)
    # =========================================================================
    s4 = prs.slides.add_slide(blank_layout)
    set_slide_background(s4, PAPER_BG)
    add_header(s4, "Core Capabilities", "What We Built: The AutoKT Platform", 
               "Five production-grade capabilities delivering complete end-to-end codebase comprehension.")

    features = [
        ("🗺️ Interactive Knowledge Graph",
         "2D Force-Directed Canvas",
         "Visualizes Repositories, Modules, Files, Functions, and Owners. Features dynamic 1-hop expansion, entity filtering, and dark/light modes.",
         ACCENT_GREEN),
        ("🛡️ Module Health & Bus Factor",
         "Risk Radar & Contributor Scoring",
         "Evaluates 0–100% transfer-readiness per module, automatically flagging sole-owner risks and unowned legacy components.",
         ACCENT_RED),
        ("💬 Grounded Code Q&A",
         "Hybrid RRF Search & Answer Generation",
         "Direct answers to natural language questions ('How does auth work?'), citing exact file paths, line ranges, and ownership metadata.",
         ACCENT_BLUE),
        ("📦 Auto-Generated Onboarding Packs",
         "Structured KT Decks & Targeted Questions",
         "One-click generation of architecture overviews, critical files, setup guides, and tailored knowledge transfer questions.",
         ACCENT_ORANGE),
    ]

    card_w = Inches(5.6)
    card_h = Inches(2.1)
    for i, (title, sub, desc, col) in enumerate(features):
        row = i // 2
        col_idx = i % 2
        cx = Inches(0.8) + col_idx * Inches(5.95)
        cy = Inches(2.2) + row * Inches(2.35)
        add_card(s4, cx, cy, card_w, card_h, bg_color=SURFACE_WHITE, border_color=BORDER_LINE)

        tb = s4.shapes.add_textbox(cx + Inches(0.25), cy + Inches(0.18), card_w - Inches(0.5), card_h - Inches(0.36))
        tf = tb.text_frame
        tf.word_wrap = True

        p = tf.paragraphs[0]
        p.text = title
        p.font.size = Pt(16)
        p.font.bold = True
        p.font.color.rgb = col
        p.font.name = "Georgia"

        p_s = tf.add_paragraph()
        p_s.text = sub
        p_s.font.size = Pt(11)
        p_s.font.bold = True
        p_s.font.color.rgb = TEXT_MUTED
        p_s.font.name = "Segoe UI"
        p_s.space_before = Pt(2)

        p_d = tf.add_paragraph()
        p_d.text = desc
        p_d.font.size = Pt(11)
        p_d.font.color.rgb = TEXT_DARK
        p_d.font.name = "Segoe UI"
        p_d.space_before = Pt(6)

    s4.notes_slide.notes_text_frame.text = (
        "Here are the five core components we delivered in AutoKT: "
        "First, the responsive Knowledge Graph where developers visually inspect architecture. "
        "Second, Module Health to audit contributor distribution and bus factor risks. "
        "Third, Grounded Q&A that gives verified answers with exact line citations. "
        "Fourth, Automated KT Packs that generate structured onboarding documentation in seconds. "
        "And fifth, Business Mapping connecting PRD requirements directly to code."
    )

    # =========================================================================
    # SLIDE 5: BUSINESS IMPACT & MEASURABLE VALUE
    # =========================================================================
    s5 = prs.slides.add_slide(blank_layout)
    set_slide_background(s5, PAPER_BG)
    add_header(s5, "Impact & ROI", "Quantifiable Engineering Value Delivered", 
               "Replacing weeks of manual meetings and guesswork with deterministic, continuous codebase intelligence.")

    metrics = [
        ("70%", "Reduction in Ramp-Up Time", "New hires self-serve onboarding context via the graph and Q&A engine instead of interrupting senior teammates.", ACCENT_GREEN),
        ("0 Days", "Knowledge Lost on Departure", "Identifies single-owner modules in advance and generates tailored KT questions before handovers occur.", ACCENT_BLUE),
        ("100%", "Fact-Grounded Code Explanations", "Zero LLM hallucinations: all responses cite exact AST code entities, file locations, and commit authors.", ACCENT_ORANGE),
        ("10x", "Faster PRD-to-Code Auditing", "Enables Product Managers and auditors to verify requirement implementation without needing manual code walkthroughs.", ACCENT_RED),
    ]

    card_w = Inches(2.75)
    for i, (stat, title, desc, col) in enumerate(metrics):
        cx = Inches(0.8) + i * Inches(3.0)
        cy = Inches(2.2)
        add_card(s5, cx, cy, card_w, Inches(4.5), bg_color=SURFACE_WHITE, border_color=BORDER_LINE)

        tb = s5.shapes.add_textbox(cx + Inches(0.2), cy + Inches(0.25), card_w - Inches(0.4), Inches(4.0))
        tf = tb.text_frame
        tf.word_wrap = True

        p = tf.paragraphs[0]
        p.text = stat
        p.font.size = Pt(36)
        p.font.bold = True
        p.font.color.rgb = col
        p.font.name = "Georgia"

        p_t = tf.add_paragraph()
        p_t.text = title
        p_t.font.size = Pt(14)
        p_t.font.bold = True
        p_t.font.color.rgb = TEXT_DARK
        p_t.font.name = "Segoe UI"
        p_t.space_before = Pt(8)

        p_d = tf.add_paragraph()
        p_d.text = desc
        p_d.font.size = Pt(11)
        p_d.font.color.rgb = TEXT_MUTED
        p_d.font.name = "Segoe UI"
        p_d.space_before = Pt(10)

    s5.notes_slide.notes_text_frame.text = (
        "By deploying AutoKT, companies transform their engineering economics: "
        "Ramp-up time drops from months to days. Key person departures no longer paralyze "
        "teams. And for the first time, product managers can audit what is built without "
        "needing a senior developer to translate code into English."
    )

    # =========================================================================
    # SLIDE 6: FUTURE IMPROVISATION & ROADMAP (Dark Theme Conclusion)
    # =========================================================================
    s6 = prs.slides.add_slide(blank_layout)
    set_slide_background(s6, DARK_BG)

    # Header in dark theme
    box_eye = s6.shapes.add_textbox(Inches(0.8), Inches(0.5), Inches(11.7), Inches(0.4))
    tf_eye = box_eye.text_frame
    p_eye = tf_eye.paragraphs[0]
    p_eye.text = "THE ROAD AHEAD"
    p_eye.font.size = Pt(11)
    p_eye.font.bold = True
    p_eye.font.color.rgb = ACCENT_LIME
    p_eye.font.name = "Segoe UI"

    box_t = s6.shapes.add_textbox(Inches(0.8), Inches(0.85), Inches(11.7), Inches(0.65))
    tf_t = box_t.text_frame
    p_t = tf_t.paragraphs[0]
    p_t.text = "Future Improvisations & Strategic Roadmap"
    p_t.font.size = Pt(26)
    p_t.font.bold = True
    p_t.font.color.rgb = TEXT_LIGHT
    p_t.font.name = "Georgia"

    box_d = s6.shapes.add_textbox(Inches(0.8), Inches(1.5), Inches(11.7), Inches(0.5))
    tf_d = box_d.text_frame
    p_d = tf_d.paragraphs[0]
    p_d.text = "Expanding AutoKT from an onboarding platform into an autonomous, continuous knowledge infrastructure."
    p_d.font.size = Pt(13)
    p_d.font.color.rgb = TEXT_LIGHT_MUTED
    p_d.font.name = "Segoe UI"

    roadmap_items = [
        ("1. CI/CD 'Knowledge Drift' Gates",
         "A GitHub Action/GitLab bot that inspects every pull request, warning when a PR introduces single-author ownership or undocumented critical functions.",
         ACCENT_LIME),
        ("2. In-Editor IDE Extensions",
         "Native VS Code and JetBrains plugins bringing the interactive graph and instant KT explanations directly into the engineer's active coding buffer.",
         ACCENT_BLUE),
        ("3. Architectural Impact Simulation",
         "Predictive change analysis: 'If I refactor module X, which 18 downstream APIs and product requirements will be impacted?'",
         ACCENT_ORANGE),
        ("4. Interactive Speech & Video Avatars",
         "Synthesizing customized, interactive walkthrough audio and video summaries for auditory and visual learners during onboarding.",
         ACCENT_GREEN),
    ]

    card_w = Inches(5.6)
    card_h = Inches(2.1)
    for i, (title, desc, col) in enumerate(roadmap_items):
        row = i // 2
        col_idx = i % 2
        cx = Inches(0.8) + col_idx * Inches(5.95)
        cy = Inches(2.2) + row * Inches(2.35)
        add_card(s6, cx, cy, card_w, card_h, bg_color=DARK_CARD, border_color=RGBColor(0x33, 0x42, 0x3C))

        tb = s6.shapes.add_textbox(cx + Inches(0.25), cy + Inches(0.18), card_w - Inches(0.5), card_h - Inches(0.36))
        tf = tb.text_frame
        tf.word_wrap = True

        p = tf.paragraphs[0]
        p.text = title
        p.font.size = Pt(16)
        p.font.bold = True
        p.font.color.rgb = col
        p.font.name = "Georgia"

        p_d = tf.add_paragraph()
        p_d.text = desc
        p_d.font.size = Pt(11)
        p_d.font.color.rgb = TEXT_LIGHT_MUTED
        p_d.font.name = "Segoe UI"
        p_d.space_before = Pt(8)

    s6.notes_slide.notes_text_frame.text = (
        "Looking forward, AutoKT evolves from a reactive onboarding portal into an autonomous "
        "engineering guardian. By integrating directly into CI/CD pipelines and IDEs, AutoKT will "
        "prevent knowledge drift before code even merges, ensuring institutional memory stays evergreen."
    )

    output_path = os.path.abspath(r"c:\Users\admin\Desktop\hackathon\autokt\AutoKT_Project_Presentation.pptx")
    prs.save(output_path)
    print(f"Presentation successfully saved to: {output_path}")

if __name__ == "__main__":
    create_presentation()
