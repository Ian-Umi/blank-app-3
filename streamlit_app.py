
import io
import os
import re
import tempfile
from pathlib import Path

import fitz  # PyMuPDF
import streamlit as st
from PIL import Image, ImageOps
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


st.set_page_config(page_title="수학 숙제장 생성기", layout="wide")

# -----------------------------
# 기본 폴더
# -----------------------------
BASE_DIR = Path(__file__).parent
WORK_DIR = BASE_DIR / "work"
EXTRACTED_DIR = WORK_DIR / "extracted"
EXPORTS_DIR = WORK_DIR / "exports"
for d in [WORK_DIR, EXTRACTED_DIR, EXPORTS_DIR]:
    d.mkdir(parents=True, exist_ok=True)


# -----------------------------
# 폰트: PDF에 한국어가 깨지지 않도록 시도
# -----------------------------
def get_pdf_font():
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",
        "C:/Windows/Fonts/malgun.ttf",
    ]
    for font_path in candidates:
        if os.path.exists(font_path):
            try:
                pdfmetrics.registerFont(TTFont("KoreanFont", font_path))
                return "KoreanFont"
            except Exception:
                pass
    return "Helvetica"


PDF_FONT = get_pdf_font()


# -----------------------------
# 이미지 유틸
# -----------------------------
def render_pdf_pages(pdf_bytes, start_page, end_page, zoom=2.0):
    """PDF를 이미지 리스트로 변환. start_page/end_page는 1부터 시작."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total = len(doc)
    start = max(1, int(start_page))
    end = min(total, int(end_page))
    images = []

    for page_no in range(start - 1, end):
        page = doc[page_no]
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        images.append((page_no + 1, img))

    doc.close()
    return images


def crop_page_margins(img, top_ratio=0.0, bottom_ratio=0.0, left_ratio=0.0, right_ratio=0.0):
    w, h = img.size
    left = int(w * left_ratio)
    right = int(w * (1 - right_ratio))
    top = int(h * top_ratio)
    bottom = int(h * (1 - bottom_ratio))
    if right <= left or bottom <= top:
        return img
    return img.crop((left, top, right, bottom))


def trim_whitespace(img, margin=18, threshold=245):
    """흰 여백 제거. 문제 내부 여백까지 과하게 자르지 않게 여유 margin 유지."""
    gray = img.convert("L")
    # 흰색이 아닌 픽셀 찾기
    mask = gray.point(lambda p: 255 if p < threshold else 0)
    bbox = mask.getbbox()
    if bbox is None:
        return img

    left, top, right, bottom = bbox
    left = max(left - margin, 0)
    top = max(top - margin, 0)
    right = min(right + margin, img.width)
    bottom = min(bottom + margin, img.height)
    return img.crop((left, top, right, bottom))


def split_grid(img, rows=1, cols=2, gap_ratio=0.012, trim=True):
    """페이지 이미지를 rows x cols로 균등 분할."""
    w, h = img.size
    pieces = []
    gap_x = int(w * gap_ratio)
    gap_y = int(h * gap_ratio)

    for r in range(rows):
        for c in range(cols):
            x0 = int(w * c / cols)
            x1 = int(w * (c + 1) / cols)
            y0 = int(h * r / rows)
            y1 = int(h * (r + 1) / rows)

            # 중앙 구분선이 같이 들어가면 여백 제거가 망가지는 경우가 있어서 살짝 뺌
            if c > 0:
                x0 += gap_x
            if c < cols - 1:
                x1 -= gap_x
            if r > 0:
                y0 += gap_y
            if r < rows - 1:
                y1 -= gap_y

            piece = img.crop((x0, y0, x1, y1))
            if trim:
                piece = trim_whitespace(piece)
            pieces.append(piece)
    return pieces


# -----------------------------
# 문제번호 기반 추출
# -----------------------------
def find_number_blocks(page):
    """
    텍스트 기반 PDF에서 '1.', '2)', '3번' 같은 문제번호 후보를 찾음.
    스캔본/이미지 PDF는 텍스트가 없어서 작동하지 않을 수 있음.
    """
    blocks = page.get_text("blocks")
    candidates = []

    # 시작 부분의 문제번호만 잡음
    pattern = re.compile(r"^\s*(?:\(?)(\d{1,3})(?:[\.\)]|번)\s*")

    for block in blocks:
        x0, y0, x1, y1, text, *_ = block
        first_line = text.strip().split("\n")[0].strip()
        m = pattern.match(first_line)
        if not m:
            continue
        num = int(m.group(1))

        # 너무 큰 번호, 이상한 머리말 방지
        if 1 <= num <= 999:
            candidates.append({
                "num": num,
                "bbox": (x0, y0, x1, y1),
                "x0": x0,
                "y0": y0,
                "x1": x1,
                "y1": y1,
            })

    return candidates


def extract_by_problem_numbers(pdf_bytes, start_page, end_page, zoom=2.0, top_ratio=0.0, bottom_ratio=0.0,
                               left_ratio=0.0, right_ratio=0.0, columns=2, gap_ratio=0.012, padding_px=20):
    """
    문제번호 위치를 기준으로 문제 영역 추출.
    - 텍스트 PDF에서만 비교적 잘 됨.
    - columns=1 또는 2 지원.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total = len(doc)
    start = max(1, int(start_page))
    end = min(total, int(end_page))
    extracted = []

    for page_no in range(start - 1, end):
        page = doc[page_no]
        page_rect = page.rect
        pw, ph = page_rect.width, page_rect.height

        # 렌더링 이미지
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        iw, ih = img.size

        # 사용자가 지정한 큰 여백 제거 영역을 PDF 좌표/이미지 좌표 둘 다로 계산
        crop_x0_img = int(iw * left_ratio)
        crop_x1_img = int(iw * (1 - right_ratio))
        crop_y0_img = int(ih * top_ratio)
        crop_y1_img = int(ih * (1 - bottom_ratio))

        crop_x0_pdf = pw * left_ratio
        crop_x1_pdf = pw * (1 - right_ratio)
        crop_y0_pdf = ph * top_ratio
        crop_y1_pdf = ph * (1 - bottom_ratio)

        candidates = find_number_blocks(page)
        # 여백 밖 후보 제거
        candidates = [
            c for c in candidates
            if crop_x0_pdf <= c["x0"] <= crop_x1_pdf and crop_y0_pdf <= c["y0"] <= crop_y1_pdf
        ]

        if not candidates:
            continue

        if columns == 1:
            col_bounds_pdf = [(crop_x0_pdf, crop_x1_pdf)]
            col_bounds_img = [(crop_x0_img, crop_x1_img)]
        else:
            mid_pdf = (crop_x0_pdf + crop_x1_pdf) / 2
            mid_img = (crop_x0_img + crop_x1_img) / 2
            gap_pdf = pw * gap_ratio
            gap_img = iw * gap_ratio
            col_bounds_pdf = [
                (crop_x0_pdf, mid_pdf - gap_pdf),
                (mid_pdf + gap_pdf, crop_x1_pdf),
            ]
            col_bounds_img = [
                (crop_x0_img, int(mid_img - gap_img)),
                (int(mid_img + gap_img), crop_x1_img),
            ]

        for col_idx, (cx0_pdf, cx1_pdf) in enumerate(col_bounds_pdf):
            col_candidates = [
                c for c in candidates
                if cx0_pdf <= (c["x0"] + c["x1"]) / 2 <= cx1_pdf
            ]
            col_candidates.sort(key=lambda c: c["y0"])

            for i, c in enumerate(col_candidates):
                y0_pdf = max(c["y0"] - (padding_px / zoom), crop_y0_pdf)
                if i + 1 < len(col_candidates):
                    y1_pdf = max(col_candidates[i + 1]["y0"] - (padding_px / zoom), y0_pdf + 10)
                else:
                    y1_pdf = crop_y1_pdf

                # PDF 좌표 -> 이미지 좌표
                x0_img = int(col_bounds_img[col_idx][0])
                x1_img = int(col_bounds_img[col_idx][1])
                y0_img = int(y0_pdf * zoom)
                y1_img = int(y1_pdf * zoom)

                # 이미지 범위 제한
                x0_img = max(0, min(x0_img, iw - 1))
                x1_img = max(1, min(x1_img, iw))
                y0_img = max(0, min(y0_img, ih - 1))
                y1_img = max(1, min(y1_img, ih))

                if x1_img > x0_img and y1_img > y0_img:
                    piece = img.crop((x0_img, y0_img, x1_img, y1_img))
                    piece = trim_whitespace(piece)
                    extracted.append((page_no + 1, c["num"], piece))

    doc.close()
    # 페이지 순서 + 문제번호 순서
    extracted.sort(key=lambda x: (x[0], x[1]))
    return extracted



# -----------------------------
# 자동 배치 감지
# -----------------------------
def _low_ink_gap_exists(mask, axis, center_min=0.35, center_max=0.65, threshold_ratio=0.08, min_width_ratio=0.015):
    """
    mask: 0/255 이미지 배열 비슷한 PIL mask.
    axis=0이면 세로 방향 구분선 후보, axis=1이면 가로 방향 구분선 후보.
    """
    import numpy as np

    arr = np.array(mask) > 0
    if arr.size == 0:
        return False

    if axis == 0:
        density = arr.mean(axis=0)
        length = arr.shape[1]
    else:
        density = arr.mean(axis=1)
        length = arr.shape[0]

    start = int(length * center_min)
    end = int(length * center_max)
    if end <= start:
        return False

    region = density[start:end]
    low = region < threshold_ratio

    min_width = max(3, int(length * min_width_ratio))
    run = 0
    for val in low:
        if val:
            run += 1
            if run >= min_width:
                return True
        else:
            run = 0
    return False


def detect_page_layout(body_img):
    """
    아주 단순한 자동 판별.
    완벽한 AI 판별은 아니고, 먼저 귀찮음을 줄여주는 1차 자동 추정입니다.
    """
    gray = body_img.convert("L")
    mask = gray.point(lambda p: 255 if p < 245 else 0)

    vertical_gap = _low_ink_gap_exists(mask, axis=0, center_min=0.42, center_max=0.58)
    horizontal_gap = _low_ink_gap_exists(mask, axis=1, center_min=0.40, center_max=0.60)

    # 중앙 세로 구분 + 중앙 가로 구분이 모두 뚜렷하면 2x2로 추정
    if vertical_gap and horizontal_gap:
        return "2x2 4문제"

    # 중앙 세로 구분만 뚜렷하면 좌우 2단
    if vertical_gap:
        return "좌우 2단"

    # 중앙 가로 구분만 있으면 위/아래형. 일단 세로 4문제보다 페이지 전체가 더 안전함.
    # 그래도 사용자가 원하면 수동으로 세로 4문제를 고르면 됨.
    if horizontal_gap:
        return "세로 4문제"

    return "페이지 전체를 한 문제로"



# -----------------------------
# 추출 모드
# -----------------------------
def extract_problem_images(
    pdf_bytes,
    mode,
    prefix,
    start_page,
    end_page,
    top_ratio,
    bottom_ratio,
    left_ratio,
    right_ratio,
    number_columns,
    zoom=2.0,
):
    saved = []
    counter = st.session_state.get("_next_problem_counter", 1)

    if mode == "문제번호 기준(텍스트 PDF)":
        extracted = extract_by_problem_numbers(
            pdf_bytes=pdf_bytes,
            start_page=start_page,
            end_page=end_page,
            zoom=zoom,
            top_ratio=top_ratio,
            bottom_ratio=bottom_ratio,
            left_ratio=left_ratio,
            right_ratio=right_ratio,
            columns=number_columns,
        )
        for page_no, problem_no, img in extracted:
            filename = f"{prefix}-{counter:03d}_p{page_no}_n{problem_no}.png"
            path = EXTRACTED_DIR / filename
            img.save(path)
            saved.append({"id": f"{prefix}-{counter:03d}", "page": page_no, "source_no": problem_no, "path": str(path)})
            counter += 1
        st.session_state["_next_problem_counter"] = counter
        return saved

    pages = render_pdf_pages(pdf_bytes, start_page, end_page, zoom=zoom)

    for page_no, page_img in pages:
        body = crop_page_margins(
            page_img,
            top_ratio=top_ratio,
            bottom_ratio=bottom_ratio,
            left_ratio=left_ratio,
            right_ratio=right_ratio,
        )

        chosen_mode = mode
        if mode == "자동 추천(먼저 이걸로)":
            chosen_mode = detect_page_layout(body)

        if chosen_mode == "좌우 2단":
            pieces = split_grid(body, rows=1, cols=2, trim=True)
        elif chosen_mode == "2x2 4문제":
            pieces = split_grid(body, rows=2, cols=2, trim=True)
        elif chosen_mode == "세로 4문제":
            pieces = split_grid(body, rows=4, cols=1, trim=True)
        elif chosen_mode == "페이지 전체를 한 문제로":
            pieces = [trim_whitespace(body)]
        else:
            pieces = split_grid(body, rows=1, cols=2, trim=True)

        for piece in pieces:
            filename = f"{prefix}-{counter:03d}_p{page_no}.png"
            path = EXTRACTED_DIR / filename
            piece.save(path)
            saved.append({"id": f"{prefix}-{counter:03d}", "page": page_no, "source_no": "", "path": str(path)})
            counter += 1

    st.session_state["_next_problem_counter"] = counter
    return saved


# -----------------------------
# 숙제장 PDF 생성
# -----------------------------
def draw_solution_lines(c, x, y, width, height, line_gap=8):
    current = y + height - 10
    while current > y + 8:
        c.line(x, current, x + width, current)
        current -= line_gap


def draw_problem_slot(c, img_path, display_no, x, y, w, h, solution_ratio=0.55):
    c.setFont(PDF_FONT, 10)
    c.drawString(x, y + h - 12, f"[{display_no}]")

    img = Image.open(img_path)
    iw, ih = img.size

    img_area_h = h * (1 - solution_ratio) - 16
    sol_area_h = h * solution_ratio - 20

    max_img_w = w
    max_img_h = max(30, img_area_h)

    scale = min(max_img_w / iw, max_img_h / ih)
    draw_w = iw * scale
    draw_h = ih * scale

    img_x = x
    img_y = y + h - 24 - draw_h
    c.drawImage(img_path, img_x, img_y, width=draw_w, height=draw_h, preserveAspectRatio=True, anchor="nw")

    # 풀이 공간
    sol_top = img_y - 12
    c.setFont(PDF_FONT, 8)
    c.drawString(x, sol_top, "풀이")
    draw_solution_lines(c, x, y + 20, w, max(10, sol_top - (y + 28)))

    c.setFont(PDF_FONT, 7)
    c.drawString(x, y + 5, "□ 혼자 풂   □ 해설 봄   □ 틀림   □ 다시 풀기")


def make_homework_pdf(problem_items, output_name, start_index=1, count=10, per_page=2, title="수학 숙제장"):
    selected = problem_items[start_index - 1: start_index - 1 + count]
    output_path = EXPORTS_DIR / output_name

    c = canvas.Canvas(str(output_path), pagesize=A4)
    page_w, page_h = A4
    margin = 14 * mm
    header_h = 15 * mm

    if per_page == 4:
        rows, cols = 2, 2
    else:
        rows, cols = 2, 1

    slots_per_page = rows * cols
    slot_w = (page_w - 2 * margin - (cols - 1) * 8 * mm) / cols
    usable_h = page_h - 2 * margin - header_h
    slot_h = (usable_h - (rows - 1) * 8 * mm) / rows

    for page_start in range(0, len(selected), slots_per_page):
        c.setFont(PDF_FONT, 14)
        c.drawString(margin, page_h - margin, title)
        c.setFont(PDF_FONT, 8)
        c.drawString(margin, page_h - margin - 13, f"문제 {start_index + page_start}번부터 / 총 {len(selected)}문제")

        page_items = selected[page_start: page_start + slots_per_page]

        for idx, item in enumerate(page_items):
            r = idx // cols
            col = idx % cols
            x = margin + col * (slot_w + 8 * mm)
            # 위에서 아래로 배치
            y = margin + (rows - 1 - r) * (slot_h + 8 * mm)
            display_no = start_index + page_start + idx
            draw_problem_slot(c, item["path"], display_no, x, y, slot_w, slot_h)

        c.showPage()

    c.save()
    return str(output_path)


# -----------------------------
# UI
# -----------------------------
st.title("수학 숙제장 생성기")
st.caption("먼저 자동 추천으로 문제를 뽑고, 이상한 구간만 보정해서 A4 숙제장으로 다시 배치합니다.")

with st.sidebar:
    st.header("1. PDF 범위")
    uploaded_file = st.file_uploader("PDF 파일 업로드", type=["pdf"])

    accumulate = st.checkbox("기존 추출 결과에 이어서 추가", value=True)
    clear_clicked = st.button("추출 결과 전체 비우기")
    if clear_clicked:
        for p in EXTRACTED_DIR.glob("*.png"):
            try:
                p.unlink()
            except Exception:
                pass
        st.session_state["problem_items"] = []
        st.session_state["_next_problem_counter"] = 1
        st.success("추출 결과를 비웠습니다.")

    prefix = st.text_input("문제 ID 접두어", value="MATH")

    start_page = st.number_input("시작 페이지", min_value=1, value=1, step=1)
    end_page = st.number_input("끝 페이지", min_value=1, value=1, step=1)

    st.header("2. 추출 방식")
    mode = st.selectbox(
        "PDF 안의 문제 배치 형태",
        [
            "자동 추천(먼저 이걸로)",
            "좌우 2단",
            "2x2 4문제",
            "세로 4문제",
            "문제번호 기준(텍스트 PDF)",
            "페이지 전체를 한 문제로",
        ],
        index=0,
    )

    number_columns = 2
    if mode == "문제번호 기준(텍스트 PDF)":
        number_columns = st.selectbox("문제번호 기준 열 수", [1, 2], index=1)
        st.info("이 모드는 텍스트가 살아있는 PDF에서만 잘 됩니다. 스캔본이면 다른 분할 모드를 쓰세요.")

    st.header("3. 여백 조정")
    top_ratio = st.slider("상단 제거 비율", 0.00, 0.40, 0.18, 0.01)
    bottom_ratio = st.slider("하단 제거 비율", 0.00, 0.30, 0.02, 0.01)
    left_ratio = st.slider("왼쪽 제거 비율", 0.00, 0.20, 0.02, 0.01)
    right_ratio = st.slider("오른쪽 제거 비율", 0.00, 0.20, 0.02, 0.01)

    st.header("4. 출력 설정")
    start_index = st.number_input("출력 시작 문제 번호", min_value=1, value=1, step=1)
    output_count = st.number_input("이번 숙제에 넣을 문제 수", min_value=1, value=10, step=1)
    per_page = st.selectbox("A4 한 장에 넣을 문제 수", [2, 4], index=0)
    output_title = st.text_input("숙제장 제목", value="수학 숙제장")


col_main, col_help = st.columns([2, 1])

with col_help:
    st.subheader("어떻게 고르나요?")
    st.markdown(
        """
- **자동 추천**: 먼저 이걸로 돌려보고, 이상한 페이지만 다른 방식으로 다시 추출.
- **좌우 2단**: 한 페이지에 왼쪽/오른쪽 2문제.
- **2x2 4문제**: 한 페이지에 위아래 2줄, 좌우 2칸.
- **세로 4문제**: 한 페이지에 세로로 4문제.
- **문제번호 기준**: 모의고사처럼 문제번호가 텍스트로 잡히는 PDF.
- **페이지 전체**: 한 페이지를 통째로 한 문제 카드처럼 저장.

처음에는 1페이지만 테스트해서 잘리는 모양을 보고, 그다음 전체 범위로 넓히세요.

가장 편한 사용법:
1. 먼저 **자동 추천**으로 전체 또는 일부를 추출
2. 미리보기에서 이상한 구간만 확인
3. 이상한 구간은 **추출 결과 전체 비우기** 또는 이어서 추가 설정을 조절해서 다시 추출
4. 마지막에 원하는 문제 수만 골라 숙제장 PDF 생성

PDF 안에 여러 배치가 많이 섞여 있으면 자동 추천이 전부 맞추지는 못합니다.
그때만 1~10쪽 좌우 2단, 11~15쪽 2x2처럼 구간별로 보정하면 됩니다.
        """
    )

with col_main:
    if not uploaded_file:
        st.warning("먼저 PDF 파일을 업로드하세요.")
    else:
        pdf_bytes = uploaded_file.read()

        if st.button("문제 추출하기", type="primary"):
            if not accumulate:
                for p in EXTRACTED_DIR.glob("*.png"):
                    try:
                        p.unlink()
                    except Exception:
                        pass
                st.session_state["problem_items"] = []
                st.session_state["_next_problem_counter"] = 1

            existing_items = st.session_state.get("problem_items", [])
            with st.spinner("PDF에서 문제를 추출하는 중입니다..."):
                new_items = extract_problem_images(
                    pdf_bytes=pdf_bytes,
                    mode=mode,
                    prefix=prefix,
                    start_page=start_page,
                    end_page=end_page,
                    top_ratio=top_ratio,
                    bottom_ratio=bottom_ratio,
                    left_ratio=left_ratio,
                    right_ratio=right_ratio,
                    number_columns=number_columns,
                    zoom=2.0,
                )
                st.session_state["problem_items"] = existing_items + new_items

            if new_items:
                st.success(f"{len(new_items)}개 문제 이미지를 새로 추출했습니다. 현재 총 {len(st.session_state['problem_items'])}개입니다.")
            else:
                st.error("추출된 문제가 없습니다. 페이지 범위, 추출 방식, 여백 비율을 바꿔보세요.")

    if "problem_items" in st.session_state and st.session_state["problem_items"]:
        items = st.session_state["problem_items"]

        st.subheader("추출 결과 미리보기")
        st.write(f"총 {len(items)}개 문제")

        preview_n = min(12, len(items))
        cols = st.columns(3)
        for i in range(preview_n):
            item = items[i]
            with cols[i % 3]:
                caption = f"{i+1}. {item['id']} / p.{item['page']}"
                if item.get("source_no"):
                    caption += f" / 원문 {item['source_no']}번"
                st.image(item["path"], caption=caption, use_container_width=True)

        st.divider()

        if start_index > len(items):
            st.error("출력 시작 문제 번호가 추출된 문제 개수보다 큽니다.")
        else:
            max_count = len(items) - start_index + 1
            if output_count > max_count:
                st.warning(f"현재 시작 번호 기준으로 최대 {max_count}문제까지 출력할 수 있습니다.")

            if st.button("숙제장 PDF 만들기"):
                actual_count = min(output_count, max_count)
                with st.spinner("숙제장 PDF를 만드는 중입니다..."):
                    out_path = make_homework_pdf(
                        items,
                        output_name="math_homework.pdf",
                        start_index=start_index,
                        count=actual_count,
                        per_page=per_page,
                        title=output_title,
                    )

                with open(out_path, "rb") as f:
                    st.download_button(
                        "숙제장 PDF 다운로드",
                        data=f,
                        file_name="math_homework.pdf",
                        mime="application/pdf",
                    )

        with st.expander("추출된 문제 목록 보기"):
            for i, item in enumerate(items, start=1):
                st.write(f"{i}. {item['id']} / page {item['page']} / {Path(item['path']).name}")
