"""
preprocessor.py
역할: PDF를 페이지별 이미지로 변환하고 전처리
교체 가능: 해상도, 전처리 방식 등 조정 가능
"""

import fitz
import cv2
import numpy as np
from PIL import Image

def preprocess_image(pix):
    """
    PDF 페이지 픽셀을 전처리된 PIL 이미지로 변환
    - 그레이스케일 변환
    - 노이즈 제거
    - 대비 향상
    - 이진화
    - 기울기 보정 (허프 변환)
    - 리사이즈
    """
    img_array = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n
    )
    if pix.n == 4:
        img_array = cv2.cvtColor(img_array, cv2.COLOR_RGBA2RGB)

    # 그레이스케일
    gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)

    # 노이즈 제거 (최소화 — 체크/필기 디테일 보존)
    denoised = cv2.GaussianBlur(gray, (1, 1), 0)

    # 대비 향상 (CLAHE)
    clahe = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8))
    enhanced = clahe.apply(denoised)

    # 허프 변환 기울기 보정 (이진화 전에 수행)
    edges = cv2.Canny(enhanced, 50, 150, apertureSize=3)
    lines = cv2.HoughLines(edges, 1, np.pi / 180, threshold=200)
    if lines is not None:
        angles = []
        for rho, theta in lines[:20, 0]:
            angle = (theta * 180 / np.pi) - 90
            if abs(angle) < 10:
                angles.append(angle)
        if angles:
            median_angle = np.median(angles)
            if abs(median_angle) > 0.5:
                h, w = enhanced.shape
                center = (w // 2, h // 2)
                M = cv2.getRotationMatrix2D(center, median_angle, 1.0)
                enhanced = cv2.warpAffine(
                    enhanced, M, (w, h),
                    flags=cv2.INTER_CUBIC,
                    borderMode=cv2.BORDER_REPLICATE
                )

    # 이진화 제거 — 그레이스케일로 전달 (체크박스·필기 디테일 보존)
    pil_image = Image.fromarray(enhanced)
    if pil_image.width > 1800:
        ratio = 1800 / pil_image.width
        new_size = (1800, int(pil_image.height * ratio))
        pil_image = pil_image.resize(new_size, Image.LANCZOS)

    return pil_image


def split_pdf(pdf_source):
    """
    PDF를 페이지별로 분리해서 전처리된 이미지 리스트 반환
    pdf_source: 파일 경로(str) 또는 PDF 바이트(bytes)
    반환값: [{"page_num": 1, "image": PIL이미지}, ...]
    """
    if isinstance(pdf_source, bytes):
        doc = fitz.open(stream=pdf_source, filetype="pdf")
    else:
        doc = fitz.open(pdf_source)
    pages = []

    for i, page in enumerate(doc):
        mat = fitz.Matrix(200 / 72, 200 / 72)
        pix = page.get_pixmap(matrix=mat)
        pil_image = preprocess_image(pix)
        pages.append({"page_num": i + 1, "image": pil_image})
        print(f"  ✅ {i + 1}페이지 전처리 완료")

    return pages
