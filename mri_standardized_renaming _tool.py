#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MRI SeriesDescription/ProtocolName classifier for dcm2nii JSON exports.

Input:
    A text file in which each line is one SeriesDescription_ProtocolName string.

Output columns:
    series_description
    sequence_type: adc / dwi / t1 / t2 / dce / unknown
    orientation: Axial / Coronal / Sagittal / Unknown
    contrast: True/False
    fat_suppression: True/False
    scan_region: pelvis / non_pelvis
    matched_rules: short explanation for auditing

Usage:
    python classify_mri_series.py all_modalities.txt -o classified_modalities.csv
"""

from __future__ import annotations

import json
import shutil

import numpy as np
import argparse
import csv
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Tuple
import os


@dataclass
class SeriesLabel:
    series_description: str
    sequence_type: str
    orientation: str
    contrast: bool
    fat_suppression: bool
    scan_region: str
    matched_rules: str


def _normalize(s: str) -> Tuple[str, str, str]:
    """
    Return:
      raw lowercase text: keeps original separators for substring checks.
      token_text: converts many separators to spaces, keeps + - / & for contrast/body rules.
      spaced_text: additionally separates + - / & into standalone tokens.
    """
    raw = (s or "").strip()
    low = raw.lower().replace("脗虏".lower(), "虏")
    # Keep + - / & because they carry meaning in +C, C+, C-, abd+pel, p+a, etc.
    token_text = re.sub(r"[_:;,()\[\]{}]+", " ", low)
    token_text = re.sub(r"\s+", " ", token_text).strip()
    spaced_text = re.sub(r"([+\-/&])", r" \1 ", token_text)
    spaced_text = re.sub(r"\s+", " ", spaced_text).strip()
    return low, token_text, spaced_text


def _first_orientation(token_text: str) -> Tuple[str, str]:
    """
    Infer imaging plane. If several planes occur in a post-processing name,
    use the first one appearing in the text.
    """
    patterns: List[Tuple[str, List[str]]] = [
        ("Axial", [
            r"(?<![a-z0-9])o?ax(?:ial)?(?![a-z0-9])",
            r"(?<![a-z0-9])axi(?:al)?(?![a-z0-9])",
            r"(?<![a-z0-9])tra(?:ns|verse)?(?![a-z0-9])",
        ]),
        ("Coronal", [r"(?<![a-z0-9])cor(?:onal)?(?![a-z0-9])"]),
        ("Sagittal", [r"(?<![a-z0-9])o?sag(?:ittal)?(?![a-z0-9])"]),
    ]

    hits: List[Tuple[int, str, str]] = []
    for label, pats in patterns:
        for pat in pats:
            m = re.search(pat, token_text)
            if m:
                hits.append((m.start(), label, pat))
                break

    if not hits:
        return "Unknown", ""
    hits.sort(key=lambda x: x[0])
    return hits[0][1], f"orientation:{hits[0][1]}"


def _has_contrast(token_text: str, spaced_text: str) -> Tuple[bool, str]:
    """
    Detect contrast-enhanced series.
    Handles +C, C+, W+C, +CE, CE-MRA, post/venous/delayed/art/dyn/DCE.
    Excludes pure -C/C- unless there is a positive contrast marker too.
    """
    # +C / C+ and variants, including +Cx6 or +C6 sometimes used for post-contrast phases.
    plus_c = re.search(
        r"(?<![a-z0-9])\+\s*c(?:\s*x?\s*\d+|x\d+)?(?![a-z0-9])|"
        r"(?<![a-z0-9])c\s*\+(?![a-z0-9])|"
        r"(?<![a-z0-9])w\s*\+\s*c(?![a-z0-9])|"
        r"(?<![a-z0-9])\+\s*ce(?![a-z0-9])|"
        r"(?<![a-z0-9])ce(?![a-z0-9])",
        spaced_text,
    )
    phase_or_dce = re.search(
        r"(?<![a-z0-9])(post|venous|delay|dely|delayed|arterial|art|"
        r"ce[- ]?mra|tricks|dyn(?:amic)?|dce)(?![a-z0-9])",
        token_text,
    )
    subtraction = re.search(r"(?<![a-z0-9])sub(?:traction)?(?![a-z0-9])", token_text)

    if plus_c:
        return True, "contrast:+C/C+/CE"
    if phase_or_dce:
        return True, f"contrast:{phase_or_dce.group(1)}"
    if subtraction and not re.search(r"(?<![a-z0-9])pre(?![a-z0-9])", token_text):
        return True, "contrast:subtraction"

    # Examples such as LAVA-C, C-, cor c- are non-contrast.
    return False, ""


def _has_fat_suppression(token_text: str) -> Tuple[bool, str]:
    """
    Detect fat/water suppression or Dixon water/fat-suppressed-equivalent names.
    The boolean is intentionally broad because real DIXON exports may say:
      FS, Fat Sat, SPAIR, SPIR, STIR/TIRM, WATER:, WATER image, DIXON/mDIXON, IDEAL, WATS, WFI.
    """
    # Strong fat-suppression terms.
    strong = re.search(
        r"(?<![a-z0-9])(fs|sfs|fat\s*sat|fatsat|spair|spir|stir|tirm|"
        r"water\s*supp(?:ression)?|water)(?![a-z0-9])",
        token_text,
    )
    if strong:
        return True, f"fat_suppression:{strong.group(1)}"

    # Dixon/IDEAL/LAVA-Flex families: water images are fat-suppressed-equivalent,
    # and protocol names often omit the explicit WATER token.
    # dixon = re.search(
    #     r"(?<![a-z0-9])(dixon|mdixon|qdixon|q[- ]dixon|ideal|lava[- ]?flex|"
    #     r"wats|wfi)(?![a-z0-9])",
    #     token_text,
    # )
    # if dixon:
    #     return True, f"fat_suppression:{dixon.group(1)}"

    return False, ""


def _scan_region(token_text: str) -> Tuple[str, str]:
    """
    Pelvis-vs-non-pelvis policy:
      - Default to pelvis.
      - Mark non_pelvis only for explicit non-pelvic sites/organs.
      - Abdomen-only is non_pelvis.
      - Abdomen+pelvis, P+A, Pel-Abd, uterus/ovary/rectum etc. are pelvis unless
        a clearly non-pelvic organ/site is present.
    """
    hard_pelvic = re.search(
        r"(?<![a-z0-9])(pelvis|uterus|uter|urerus|ovary|ovaries|rectum)(?![a-z0-9])|"
        r"zi\s*gong|luan\s*chao|female\s+pelvis|big\s+pelvis",
        token_text,
    )
    weak_pelvic_or_combined = re.search(
        r"(?<![a-z0-9])pel(?![a-z0-9])|"
        r"abd\s*[\+\-&/,]*\s*pel|pel\s*[\+\-&/,]*\s*abd|"
        r"p\s*[\+&-]\s*a|a\s*[\+&-]\s*p|pe?l?abd|abdpe?l|abdpelvis",
        token_text,
    )
    hard_non_pelvic = re.search(
        r"(?<![a-z0-9])(brain|head|spine|chest|lung|liver|pancreas|mrcp|vmrcp|"
        r"upabd|upperabd)(?![a-z0-9])|abodome",
        token_text,
    )
    abd_only = (
            re.search(r"(?<![a-z0-9])(abd|abdomen)(?![a-z0-9])", token_text)
            and not hard_pelvic
            and not weak_pelvic_or_combined
    )

    if hard_non_pelvic:
        return "non_pelvis", f"scan_region:explicit_non_pelvis:{hard_non_pelvic.group(0)}"
    if abd_only:
        return "non_pelvis", "scan_region:abdomen_only"
    return "pelvis", "scan_region:default_or_pelvic"


def _sequence_type(token_text: str) -> Tuple[str, str]:
    """
    Sequence type is priority-based to avoid ADC/DWI/T1/T2/DCE conflicts.
    Priority:
      ADC > DCE > DWI > T2 > T1 > unknown
    """
    rules: List[Tuple[str, str, str]] = [
        (
            "adc",
            r"(?<![a-z0-9])(e?adc|dadc|apparent\s+diffusion\s+coefficient)(?![a-z0-9])",
            "sequence:ADC/eADC/dADC",
        ),
        (
            "dce",
            r"(?<![a-z0-9])(dce|dyn(?:amic)?|twist[- ]?vibe|vibe[- ]?twist|"
            r"grasp|tricks|dyna\w*|4ph|3phases|ph\d+)(?![a-z0-9])",
            "sequence:DCE/dynamic",
        ),
        (
            "dwi",
            r"(?<![a-z0-9])(dwi[a-z0-9]*|d?dwibs|diff|ep2d|resolve|muse[- ]?dwi|tracew|trace|"
            r"calculated\s+bval|calc[- ]?bval)(?![a-z0-9])",
            "sequence:DWI/diffusion",
        ),
        (
            "dwi",
            r"(?<![a-z0-9])b\s*[=-]?\s*(50|400|600|800|1000|1500)(?![a-z0-9])|"
            r"(?<![a-z0-9])b(50|400|600|800|1000|1500)(?![a-z0-9])",
            "sequence:DWI:b-value",
        ),
        (
            "t2",
            r"(?<![a-z0-9])(t2[a-z0-9]*|haste|ssfse|frfse|trufi|fiesta|propeller|prop|"
            r"blade|space|mrcp|vmrcp|nervevie|nerveview|mvxd|fse[- ]?xl|tirm)(?![a-z0-9])|"
            r"t2\s*fse|t2fse|tse.*t2|t2.*tse",
            "sequence:T2",
        ),
        (
            "t1",
            r"(?<![a-z0-9])(t1[a-z0-9]*|lava|vibe|quick3d|fspgr|flash|spgr|mdixon|"
            r"dixon|thrive|tfe|ffe|gre|wats|angio3d|fl2d|fl3d|se)(?![a-z0-9])|"
            r"t1\s*fse|t1fse|tse.*t1|t1.*tse|t1[a-z0-9]*dixon|[a-z]mdixon",
            "sequence:T1",
        ),
    ]

    # A separate DCE condition for names like "T1_VIBE_SAG_FS_DYN_15".
    if re.search(r"t1.*(?<![a-z0-9])dyn", token_text):
        return "dce", "sequence:T1_dynamic"

    for label, pattern, reason in rules:
        if re.search(pattern, token_text):
            return label, reason

    return "unknown", "sequence:unknown"


def get_orientation(info):
    """
    根据 dcm2niix 生成的 json 文件中的 ImageOrientationPatientDICOM 判断方位。
    """
    # 获取方向向量
    iop = info.get("ImageOrientationPatientDICOM")
    if not iop or len(iop) != 6:
        return "Error: ImageOrientationPatientDICOM not found or invalid."

    # 行向量 (X方向) 和 列向量 (Y方向)
    row_vec = np.array(iop[0:3])
    col_vec = np.array(iop[3:6])

    # 计算法向量 (Z方向，即层方向)
    # 叉积: row x col
    normal_vec = np.cross(row_vec, col_vec)

    # 取绝对值最大的分量来判断
    abs_normal = np.abs(normal_vec)
    max_idx = np.argmax(abs_normal)

    # DICOM 坐标系定义:
    # X: Left (+) -> Right (-)
    # Y: Anterior (前+) -> Posterior (后-)
    # Z: Head (上+) -> Foot (下-)

    directions = {
        0: ("Sagittal", "Left-Right"),
        1: ("Coronal", "Anterior-Posterior"),
        2: ("Axial", "Head-Foot")
    }

    plane, axis_desc = directions[max_idx]

    return plane


def classify_one(metadata: dict) -> SeriesLabel:
    series_description = ''
    if 'SeriesDescription' in metadata.keys():
        series_description += metadata['SeriesDescription']
    if 'ProtocolName' in metadata.keys():
        series_description += '_' + metadata['ProtocolName']
    _, token_text, spaced_text = _normalize(series_description)

    sequence_type, seq_reason = _sequence_type(token_text)
    if 'ImageOrientationPatientDICOM' in metadata.keys():
        orientation = get_orientation(metadata)
        # orientation = metadata['ImageOrientationPatientDICOM']
        ori_reason = 'ImageOrientationPatientDICOM'
    else:
        orientation, ori_reason = _first_orientation(token_text)
    contrast, con_reason = _has_contrast(token_text, spaced_text)
    fat_suppression, fs_reason = _has_fat_suppression(token_text)
    scan_region, region_reason = _scan_region(token_text)

    reasons = [x for x in [seq_reason, ori_reason, con_reason, fs_reason, region_reason] if x]
    return SeriesLabel(
        series_description=series_description,
        sequence_type=sequence_type,
        orientation=orientation,
        contrast=contrast,
        fat_suppression=fat_suppression,
        scan_region=scan_region,
        matched_rules="; ".join(reasons),
    )


def normalize_mri_filename(file_path):
    """
    根据文件名判断MRI序列类型并返回规范化名称（大写）。
    规则：
    - 基础: T1 / T2
    - 增强: C (如果存在)
    - 方位: A (轴位) / S (矢状位/冠状位)
    - 组合: [T1/T2] + [C] + [A/S]
    """
    filename = file_path.name
    name_lower = filename.lower()
    base_name = filename.replace('.nii.gz', '')
    if 'ROI' in base_name:
        num = base_name.find('_ROI')
        base_name = base_name[:num]
        json_data = json.load(open(os.path.dirname(file_path) + f'/{base_name}.json'))
    elif 'Eq' in base_name:
        num = base_name.find('_Eq')
        base_name = base_name[:num]
        json_data = json.load(open(os.path.dirname(file_path) + f'/{base_name}.json'))
    else:
        if os.path.exists(str(file_path)[:-7] + '.json'):
            json_data = json.load(open(str(file_path)[:-7] + '.json'))
        else:
            json_data = {}
    series_label = classify_one(json_data)
    norm_name = series_label.scan_region + '_'
    if 'adc' in series_label.sequence_type or 'dwi' in series_label.sequence_type:
        norm_name += series_label.sequence_type.upper()
    else:
        norm_name += series_label.sequence_type.upper()
        # if series_label.contrast:
        #     norm_name += 'C'
        # if series_label.fat_suppression:
        #     norm_name += 'F'
        norm_name += '_' + series_label.orientation[:3]

    return norm_name


def process_mri_files(root_dir, save_dir, dry_run=False):
    """遍历目录并处理MRI文件"""
    if os.path.exists(save_dir):
        shutil.rmtree(save_dir)
    root_path = Path(root_dir)
    save_path = Path(save_dir)
    is_rename_mode = (root_path.resolve() == save_path.resolve())
    stats = {'total': 0, 'processed': 0, 'skipped': 0, 'errors': 0, 'by_type': {}}

    print(f"{'[预览模式]' if dry_run else '[执行模式]'} 开始扫描...")
    if not is_rename_mode:
        if not save_path.exists():
            print(f"保存目录不存在，正在创建: {save_path}")
            save_path.mkdir(parents=True, exist_ok=True)
        print(f"源目录: {root_path}\n目标目录: {save_path}")
    else:
        print(f"源目录: {root_path} (重命名模式)")
    for risk_dir in root_path.iterdir():
        if not risk_dir.is_dir():
            continue
        for patient_dir in risk_dir.iterdir():
            if not patient_dir.is_dir():
                continue

            nii_files = list(patient_dir.glob('*.nii.gz'))
            if not nii_files:
                continue

            target_patient_dir = patient_dir if is_rename_mode else save_path / risk_dir.name / patient_dir.name
            if not is_rename_mode and not target_patient_dir.exists():
                os.makedirs(target_patient_dir, exist_ok=True)

            # 按类型分组
            groups = {}
            for f in nii_files:
                seq = normalize_mri_filename(f)
                groups.setdefault(seq, []).append(f)

            for seq_type, files in groups.items():
                files.sort(key=lambda x: x.name)
                for idx, file_path in enumerate(files, 1):
                    stats['total'] += 1
                    stats['by_type'][seq_type] = stats['by_type'].get(seq_type, 0) + 1
                    new_name = f"{seq_type}_{idx:03d}.nii.gz"
                    new_name_json = f"{seq_type}_{idx:03d}.json"
                    target_file_path = target_patient_dir / new_name
                    target_json_path = target_patient_dir / new_name_json

                    if target_file_path.exists():
                        print(f"  [冲突] {target_file_path.name} 已存在，跳过 {file_path.name}")
                        stats['skipped'] += 1
                        continue

                    if is_rename_mode and file_path.name == new_name:
                        print(f"  [跳过] {file_path.name} (已是规范名称)")
                        stats['skipped'] += 1
                        continue

                    if dry_run:
                        action = "将重命名" if is_rename_mode else "将link"
                        print(f"  [{action}] {file_path.name} -> {target_file_path}")
                    else:
                        try:
                            json_path = Path(str(file_path)[:-7] + '.json')
                            if is_rename_mode:
                                file_path.rename(target_file_path)

                                if json_path.exists():
                                    json_path.rename(target_json_path)
                                print(f"  [重命名] {file_path.name} -> {new_name}")
                            else:
                                if json_path.exists():
                                    os.symlink(json_path, target_json_path)
                                os.symlink(file_path, target_file_path)
                                print(f"  [link] {file_path.name} -> {target_file_path}")
                            stats['processed'] += 1
                        except Exception as e:
                            print(f"  [错误] {e}")
                            stats['errors'] += 1
                            continue

                    if dry_run:
                        stats['processed'] += 1

    print("\n" + "=" * 50)
    print("处理统计:")
    print(f"总计: {stats['total']}, 处理: {stats['processed']}, 跳过: {stats['skipped']}, 错误: {stats['errors']}")
    print("\n分类详情:")
    for k, v in sorted(stats['by_type'].items()):
        print(f"  {k:<10}: {v}")


if __name__ == "__main__":
    root_directory = "/home/khtao/WorkCenter/PycharmProjects/风险分层论文修订/radiomics_dataset/深汕_finally_重命名v2"
    save_directory = "/home/khtao/WorkCenter/PycharmProjects/风险分层论文修订/radiomics_dataset/深汕_finally_重命名v3"

    print("MRI 规范化重命名工具 (优化版 - 减少OTHER类)")
    print("=" * 50)
    process_mri_files(root_directory, save_directory, dry_run=False)
    root_directory = "/home/khtao/WorkCenter/PycharmProjects/风险分层论文修订/radiomics_dataset/东莞人医_finally_重命名v2"
    save_directory = "/home/khtao/WorkCenter/PycharmProjects/风险分层论文修订/radiomics_dataset/东莞人医_finally_重命名v3"

    print("MRI 规范化重命名工具 (优化版 - 减少OTHER类)")
    print("=" * 50)
    process_mri_files(root_directory, save_directory, dry_run=False)

    root_directory = "/home/khtao/WorkCenter/PycharmProjects/风险分层论文修订/radiomics_dataset/佛山市一妇科_finally_重命名v2"
    save_directory = "/home/khtao/WorkCenter/PycharmProjects/风险分层论文修订/radiomics_dataset/佛山市一妇科_finally_重命名v3"

    print("MRI 规范化重命名工具 (优化版 - 减少OTHER类)")
    print("=" * 50)
    process_mri_files(root_directory, save_directory, dry_run=False)

    root_directory = "/home/khtao/WorkCenter/PycharmProjects/风险分层论文修订/radiomics_dataset/中山二院_finally_重命名v2"
    save_directory = "/home/khtao/WorkCenter/PycharmProjects/风险分层论文修订/radiomics_dataset/中山二院_finally_重命名v3"

    print("MRI 规范化重命名工具 (优化版 - 减少OTHER类)")
    print("=" * 50)
    process_mri_files(root_directory, save_directory, dry_run=False)

    root_directory = "/home/khtao/WorkCenter/PycharmProjects/风险分层论文修订/radiomics_dataset/shantou_finally_重命名v2"
    save_directory = "/home/khtao/WorkCenter/PycharmProjects/风险分层论文修订/radiomics_dataset/shantou_finally_重命名v3"

    print("MRI 规范化重命名工具 (优化版 - 减少OTHER类)")
    print("=" * 50)
    process_mri_files(root_directory, save_directory, dry_run=False)

    root_directory = "/home/khtao/WorkCenter/PycharmProjects/风险分层论文修订/radiomics_dataset/zhongshan_finally_重命名v2"
    save_directory = "/home/khtao/WorkCenter/PycharmProjects/风险分层论文修订/radiomics_dataset/zhongshan_finally_重命名v3"

    print("MRI 规范化重命名工具 (优化版 - 减少OTHER类)")
    print("=" * 50)
    process_mri_files(root_directory, save_directory, dry_run=False)
