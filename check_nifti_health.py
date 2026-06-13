import os
import SimpleITK as sitk
from concurrent.futures import ProcessPoolExecutor, as_completed
import glob


def copy_meta_data(src_img, dst_img):
    """将 src_img 的元数据复制到 dst_img (优化版)"""
    # 直接获取字典并批量设置，比逐个 SetMetaData 稍快
    keys = src_img.GetMetaDataKeys()
    if not keys:
        return
    for key in keys:
        dst_img.SetMetaData(key, src_img.GetMetaData(key))


def process_single_file(args):
    """
    处理单个文件的函数，用于多进程调用
    args: (file_path, output_dir)
    """
    file_path, output_dir = args
    fname = os.path.basename(file_path)

    # 仅处理 nii/nii.gz
    if not (fname.endswith(".nii") or fname.endswith(".nii.gz")):
        return f"跳过非NIfTI文件: {fname}"

    try:
        img = sitk.ReadImage(file_path)
    except Exception as e:
        return f"❌ 无法读取 {fname}: {e}"

    size = img.GetSize()
    ndim = len(size)

    # ---------- 情况 1：4D 图像 ----------
    if ndim == 4:
        base_name = fname.replace(".nii.gz", "").replace(".nii", "")
        num_volumes = size[3]

        # 预创建 ExtractImageFilter 以提高循环效率（虽然简单，但减少了对象创建开销）
        extractor = sitk.ExtractImageFilter()
        extractor.SetSize([size[0], size[1], size[2], 0])  # Z 维度大小为0表示提取3D

        saved_files = []
        for i in range(num_volumes):
            extractor.SetIndex([0, 0, 0, i])
            vol_img = extractor.Execute(img)

            # 复制元数据
            copy_meta_data(img, vol_img)
            vol_img.SetMetaData("dim", "3")
            if base_name.endswith('_0000'):
                out_name = f"{base_name[:-5]}_vol{i:04d}_0000.nii.gz"
            else:
                out_name = f"{base_name}_vol{i:04d}.nii.gz"
            out_path = os.path.join(output_dir, out_name)

            sitk.WriteImage(vol_img, out_path)
            saved_files.append(out_name)

            # 显式删除局部变量以帮助 GC (可选，但在大循环中有效)
            del vol_img

        # 删除原始 4D 文件
        try:
            os.remove(file_path)
        except PermissionError:
            pass  # 忽略权限错误

        return f"✅ 拆分 {fname} -> {len(saved_files)} 个 volumes"

    # ---------- 情况 2：非 4D 图像 ----------
    else:
        if any(s < 5 for s in size):
            try:
                os.remove(file_path)
                return f"🗑️  删除 2D 图像: {fname}"
            except Exception as e:
                return f"⚠️ 无法删除 {fname}: {e}"
        else:
            # 如果是正常的 3D 图像且不在输出目录，则保存（或者跳过，因为原地操作）
            # 注意：如果 input_dir == output_dir，这里不需要额外操作
            return f"➡️ 保留 3D 图像: {fname}"


def split_4d_and_clean_2d_parallel(input_dir, output_dir, max_workers=None):
    """
    并行处理版本
    :param max_workers: 并行进程数，None 表示 CPU 核心数
    """
    os.makedirs(output_dir, exist_ok=True)

    # 收集所有待处理文件
    # 使用 glob 可能比 listdir 更快，且支持递归（如果需要）
    patterns = [os.path.join(input_dir, "*.nii"), os.path.join(input_dir, "*.nii.gz")]
    files_to_process = []
    for pattern in patterns:
        files_to_process.extend(glob.glob(pattern))

    if not files_to_process:
        print("未找到任何 NIfTI 文件。")
        return

    print(f"找到 {len(files_to_process)} 个文件，开始并行处理...")

    # 准备参数列表
    task_args = [(fp, output_dir) for fp in files_to_process]

    # 使用 ProcessPoolExecutor 进行并行处理
    # 注意：如果文件非常多且很小，进程间通信开销可能抵消收益
    # 但对于大文件（4D MRI），多进程优势明显
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        results = executor.map(process_single_file, task_args)

        for res in results:
            print(res)


if __name__ == "__main__":
    # 修改为你的实际路径
    input_dir = "/home/khtao/khtao_home/Dataset/risk_dataset/3DIMG_SEG临时文件"
    output_dir = "/home/khtao/khtao_home/Dataset/risk_dataset/3DIMG_SEG临时文件"

    # 如果输入输出目录相同，意味着原地修改
    # 注意：原地修改时，多进程读取和删除同一目录下的文件通常是安全的，但建议备份
    split_4d_and_clean_2d_parallel(input_dir, output_dir, max_workers=16)
    print("🎉 处理完成！")
