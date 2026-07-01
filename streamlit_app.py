import io
import os
from datetime import date

import fitz  # PyMuPDF
import streamlit as st
from PIL import Image, ImageOps
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

APP_DIR = os.path.dirname(__file__)
EXTRACTED_DIR = os.path.join(APP_DIR, "extracted")
EXPORTS_DIR = os.path.join(APP_DIR, "exports")
os.makedirs(EXTRACTED_DIR, exist_ok=True)
os.makedirs(EXPORTS_DIR, exist_ok=True)

try:
    pdfmetrics.registerFont(UnicodeCIDFont("HYGoThic-Medium"))
    KOREAN_FONT = "HYGoThic-Medium"
except Exception:
    KOREAN_FONT = "Helvetica"


def render_pdf_pages(pdf_bytes: bytes, zoom: float = 2.0):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = []
    for page in doc:
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        pages.append(img)
    return pages


def trim_whitespace(img: Image.Image, margin: int = 18, threshold: int = 245):
    gray = img.convert("L")
    # threshold보다 어두운 픽셀만 내용으로 봄
    mask = gray.point(lambda p: 255 if p < threshold else 0)
    bbox = mask.getbbox()
    if bbox is None:
        return img
    left, top, right, bottom = bbox
    left = max(0, left - margin)
    top = max(0, top - margin)
    right = min(img.width, right + margin)
    bottom = min(img.height, bottom + margin)
    return img.crop((left, top, right, bottom))


def split_two_columns(
    page_img: Image.Image,
    top_crop_ratio: float = 0.18,
    left_margin_ratio: float = 0.03,
    right_margin_ratio: float = 0.03,
    center_gap_ratio: float = 0.012,
):
    w, h = page_img.size
    top = int(h * top_crop_ratio)
    left_margin = int(w * left_margin_ratio)
    right_margin = int(w * right_margin_ratio)
    center_gap = int(w * center_gap_ratio)
    mid = w // 2

    body = page_img.crop((left_margin, top, w - right_margin, h))
    bw, bh = body.size
    mid = bw // 2

    left = body.crop((0, 0, max(1, mid - center_gap), bh))
    right = body.crop((min(bw - 1, mid + center_gap), 0, bw, bh))

    return trim_whitespace(left), trim_whitespace(right)


def extract_problem_images(
    pdf_bytes: bytes,
    top_crop_ratio: float,
    start_page: int,
    end_page: int,
    prefix: str,
):
    pages = render_pdf_pages(pdf_bytes)
    selected_pages = pages[start_page - 1 : end_page]
    paths = []
    counter = 1

    # 이전 추출 파일과 이름 충돌 방지
    for page_index, page_img in enumerate(selected_pages, start=start_page):
        left, right = split_two_columns(page_img, top_crop_ratio=top_crop_ratio)
        for side, problem_img in [("L", left), ("R", right)]:
            filename = f"{prefix}-{counter:03d}_p{page_index}_{side}.png"
            path = os.path.join(EXTRACTED_DIR, filename)
            problem_img.save(path)
            paths.append(path)
            counter += 1
    return paths


def draw_problem_block(c, img_path, display_num, x, y, width, height):
    c.setFont(KOREAN_FONT, 11)
    c.drawString(x, y + height - 11, f"[{display_num}]")

    img = Image.open(img_path)
    iw, ih = img.size

    max_img_w = width
    max_img_h = height * 0.40
    scale = min(max_img_w / iw, max_img_h / ih, 1.0)
    draw_w = iw * scale
    draw_h = ih * scale

    img_x = x
    img_y = y + height - 20 - draw_h
    c.drawImage(img_path, img_x, img_y, width=draw_w, height=draw_h, preserveAspectRatio=True, anchor="nw")

    line_y = img_y - 18
    c.setFont(KOREAN_FONT, 9)
    c.drawString(x, line_y, "풀이:")

    current_y = line_y - 12
    while current_y > y + 24:
        c.line(x, current_y, x + width, current_y)
        current_y -= 9

    c.setFont(KOREAN_FONT, 8)
    c.drawString(x, y + 8, "□ 혼자 풂   □ 해설 봄   □ 틀림   □ 다시 풀기")


def make_homework_pdf(problem_paths, output_name, title, problems_per_day=10, problems_per_page=2):
    output_path = os.path.join(EXPORTS_DIR, output_name)
    c = canvas.Canvas(output_path, pagesize=A4)
    page_w, page_h = A4
    margin = 14 * mm
    header_h = 16 * mm

    selected = problem_paths[:problems_per_day]
    if not selected:
        raise ValueError("문제 이미지가 없습니다.")

    block_w = page_w - 2 * margin
    usable_h = page_h - 2 * margin - header_h
    block_h = usable_h / problems_per_page

    for page_start in range(0, len(selected), problems_per_page):
        c.setFont(KOREAN_FONT, 15)
        c.drawString(margin, page_h - margin, title)
        c.setFont(KOREAN_FONT, 9)
        c.drawString(margin, page_h - margin - 14, f"오늘 목표: {problems_per_day}문제 / 막히면 최소 10분 버티기")

        for slot in range(problems_per_page):
            idx = page_start + slot
            if idx >= len(selected):
                break
            y = margin + usable_h - block_h * (slot + 1)
            draw_problem_block(c, selected[idx], idx + 1, margin, y, block_w, block_h - 5 * mm)

        c.showPage()

    c.save()
    return output_path


st.set_page_config(page_title="수학 숙제장 생성기", layout="wide")
st.title("수학 숙제장 생성기")
st.write("PDF 문제지를 올리면 좌우 2단으로 문제를 잘라서 출력용 숙제장 PDF를 만듭니다.")

uploaded_file = st.file_uploader("PDF 파일 업로드", type=["pdf"])

col_a, col_b, col_c = st.columns(3)
with col_a:
    prefix = st.text_input("문제 ID 접두어", value="MATH")
with col_b:
    start_page = st.number_input("시작 페이지", min_value=1, value=1, step=1)
with col_c:
    end_page = st.number_input("끝 페이지", min_value=1, value=1, step=1)

top_crop_ratio = st.slider("상단 제목 제거 비율", 0.05, 0.35, 0.18, 0.01)

col_d, col_e = st.columns(2)
with col_d:
    problems_per_day = st.number_input("하루 문제 수", min_value=2, max_value=30, value=10, step=1)
with col_e:
    problems_per_page = st.selectbox("A4 한 장에 넣을 문제 수", [2, 4], index=0)

if uploaded_file is not None:
    pdf_bytes = uploaded_file.read()
    page_count = len(fitz.open(stream=pdf_bytes, filetype="pdf"))
    st.info(f"업로드된 PDF는 총 {page_count}페이지입니다.")

    if end_page < start_page:
        st.error("끝 페이지는 시작 페이지보다 크거나 같아야 합니다.")
    elif end_page > page_count:
        st.error("끝 페이지가 PDF 전체 페이지 수보다 큽니다.")
    else:
        if st.button("문제 자동 추출"):
            paths = extract_problem_images(pdf_bytes, top_crop_ratio, start_page, end_page, prefix)
            st.session_state["problem_paths"] = paths
            st.success(f"{len(paths)}개 문제 이미지를 추출했습니다.")

if "problem_paths" in st.session_state:
    paths = st.session_state["problem_paths"]
    st.subheader("추출 미리보기")
    preview_count = min(8, len(paths))
    cols = st.columns(2)
    for i in range(preview_count):
        with cols[i % 2]:
            st.image(paths[i], caption=os.path.basename(paths[i]))

    st.subheader("숙제장 PDF 생성")
    title = st.text_input("숙제장 제목", value=f"수학 숙제장 {date.today().isoformat()}")

    if st.button("숙제장 PDF 만들기"):
        output_path = make_homework_pdf(
            paths,
            "math_homework.pdf",
            title=title,
            problems_per_day=problems_per_day,
            problems_per_page=problems_per_page,
        )
        with open(output_path, "rb") as f:
            st.download_button(
                "숙제장 PDF 다운로드",
                data=f,
                file_name="math_homework.pdf",
                mime="application/pdf",
            )
