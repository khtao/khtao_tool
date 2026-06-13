import os
import SimpleITK as sitk
import numpy as np
from PIL import Image
import multiprocessing
from functools import partial
from khtao import list_file_tree
from scipy.ndimage import zoom

img_str = '临时文件'
seg_str = '_output'


# 重采样函数
def resample_image(image, new_spacing=(0.5, 0.5, 1.0), order=0):
    """
    将图像重采样到指定的体素间距

    Args:
        image: SimpleITK图像
        new_spacing: 目标体素间距 (x, y, z)
        interpolator: 插值方法

    Returns:
        重采样后的图像
    """
    # 获取原始图像的参数
    original_spacing = image.GetSpacing()
    scale = [ii / jj for ii, jj in zip(original_spacing, new_spacing)]
    scale = scale[::-1].copy()
    image = np.array(sitk.GetArrayFromImage(image))
    if np.argmax(original_spacing) == 1:
        image = image.transpose((1, 0, 2))
        scale = [scale[1], scale[0], scale[2]]
    if np.argmax(original_spacing) == 0:
        image = image.transpose((2, 0, 1))
        scale = [scale[2], scale[0], scale[1]]

    image = zoom(image, scale, order=order)
    return image


# 归一化函数
def normalize_image(image_array):
    """
    对图像进行归一化到[0, 255]范围

    Args:
        image_array: 图像numpy数组

    Returns:
        归一化后的图像数组
    """
    # 移除异常值（可选，根据你的数据调整）
    image_array = np.clip(image_array, np.percentile(image_array, 1), np.percentile(image_array, 99))

    # 归一化到[0, 1]
    img_min = image_array.min()
    img_max = image_array.max()

    if img_max > img_min:  # 避免除以0
        normalized = (image_array - img_min) / (img_max - img_min)
    else:
        normalized = image_array

    # 缩放到[0, 255]用于PNG保存
    normalized = (normalized * 255).astype(np.uint8)

    return normalized


# 查找分割面积最大的层
def find_max_slice(mask_array):
    """
    查找分割掩码中分割面积最大的层

    Args:
        mask_array: 掩码numpy数组，形状为(z, y, x)

    Returns:
        最大分割面积层的索引
    """
    # 计算每一层的分割面积（非零像素数量）
    slice_areas = []
    for z in range(mask_array.shape[0]):
        slice_area = np.sum(mask_array[z] > 0)
        slice_areas.append(slice_area)

    # 找到面积最大的层
    max_area_idx = np.argmax(slice_areas)

    return max_area_idx, slice_areas[max_area_idx]


# 保存灰度图像切片
def save_gray_slices(image_slice, mask_slice, output_path, image_filename):
    """
    将图像切片和掩码切片保存为灰度PNG

    Args:
        image_slice: 图像切片，2D数组
        mask_slice: 掩码切片，2D数组
        output_path: 输出路径
        image_filename: 原始图像文件名（用于命名输出文件）
    """
    # 确保输出目录存在
    os.makedirs(output_path, exist_ok=True)

    # 归一化图像切片为灰度
    image_slice_norm = normalize_image(image_slice)

    # 处理掩码切片，将值映射到0-255范围
    mask_binary = (mask_slice > 0).astype(np.uint8) * 255

    # 获取基础文件名
    base_name = os.path.splitext(os.path.basename(image_filename))[0]
    if base_name.endswith('.nii'):
        base_name = base_name[:-4]

    # 保存原始图像切片（灰度）
    img_output = os.path.join(output_path, f"{base_name}_image_slice.png")
    Image.fromarray(image_slice_norm, mode='L').save(img_output)
    print(f"保存图像切片: {img_output}")

    # 保存掩码图像（二值灰度）
    mask_output = os.path.join(output_path, f"{base_name}_mask_slice.png")
    Image.fromarray(mask_binary, mode='L').save(mask_output)
    print(f"保存掩码切片: {mask_output}")

    return img_output, mask_output


# 处理单个文件的函数，用于并行处理
def process_single_file(seg_path, root_dir, output_base_dir, target_spacing=(1.0, 1.0, 1.0)):
    """
    处理单个分割文件的函数

    Args:
        seg_path: 分割文件路径
        root_dir: 根目录
        output_base_dir: 输出基础目录
        target_spacing: 目标体素间距

    Returns:
        处理结果信息
    """
    img_path = seg_path.replace(seg_str, img_str)

    if not os.path.exists(img_path):
        img_path2 = img_path.replace('.nii.gz', '_0000.nii.gz')
        if not os.path.exists(img_path2):
            return {"file": seg_path, "status": "error", "message": f"对应图像文件不存在: {img_path}"}
        else:
            img_path = img_path2

    try:
        center_output_dir = os.path.dirname(seg_path.replace(root_dir, output_base_dir))
        metadata_path = os.path.join(center_output_dir,
                                     f"{os.path.basename(img_path).replace('.nii.gz', '')}_metadata.txt")
        if os.path.exists(metadata_path):
            return {"file": seg_path, "status": "success", "message": f"跳过已完成文件"}
        # 1. 读取图像和掩码
        mask = sitk.ReadImage(seg_path)
        img = sitk.ReadImage(img_path)

        # 2. 重采样到目标体素间距
        print(f"处理文件: {seg_path}")
        print("进行重采样...")

        # 图像使用线性插值
        img_array = resample_image(img, target_spacing, 3)

        # 分割掩码使用最近邻插值以保持二值特性
        mask_array = resample_image(mask, target_spacing, 0)

        # 3. 查找分割面积最大的层
        print("查找最大分割面积层...")
        max_slice_idx, max_area = find_max_slice(mask_array)
        print(f"  最大面积层索引: {max_slice_idx}, 面积: {max_area} 像素")

        if max_area > 0:  # 确保有分割区域
            # 4. 提取最大面积层的图像和掩码
            img_slice = img_array[max_slice_idx, :, :]
            mask_slice = mask_array[max_slice_idx, :, :]

            # 5. 创建输出目录
            os.makedirs(center_output_dir, exist_ok=True)

            # 6. 保存为灰度PNG
            print("保存灰度PNG图像...")
            save_gray_slices(img_slice, mask_slice, center_output_dir, os.path.basename(img_path))

            # 7. 保存处理元数据
            metadata_path = os.path.join(center_output_dir,
                                         f"{os.path.basename(img_path).replace('.nii.gz', '')}_metadata.txt")
            with open(metadata_path, 'w') as f:
                f.write(f"原始图像: {img_path}\n")
                f.write(f"原始分割: {seg_path}\n")
                f.write(f"原始间距: {img.GetSpacing()}\n")
                f.write(f"重采样后间距: {target_spacing}\n")
                f.write(f"最大分割面积层: {max_slice_idx}\n")
                f.write(f"分割面积: {max_area} 像素\n")
                f.write(f"图像大小: {img_slice.shape}\n")

            return {"file": seg_path, "status": "success", "message": f"处理完成，最大分割面积: {max_area}"}
        else:
            message = f"图像 {seg_path} 中没有找到分割区域"
            print(f"警告: {message}")
            return {"file": seg_path, "status": "warning", "message": message}

    except Exception as e:
        import traceback
        error_message = f"处理文件 {seg_path} 时出错: {str(e)}"
        print(error_message)
        traceback.print_exc()
        return {"file": seg_path, "status": "error", "message": error_message}


# 进度回调函数
def progress_callback(result, total_files, processed_files, lock):
    """进度回调函数"""
    with lock:
        processed_files.value += 1
        progress = processed_files.value / total_files * 100
        print(f"\r进度: {processed_files.value}/{total_files} ({progress:.1f}%) - {result['file']}: {result['status']}",
              end="")
        if processed_files.value == total_files:
            print()  # 换行


# 主处理函数
def process_files_in_parallel(root_dir, output_base_dir, num_processes=None, target_spacing=(1.0, 1.0, 1.0)):
    """
    并行处理所有文件

    Args:
        root_dir: 根目录
        output_base_dir: 输出基础目录
        num_processes: 进程数，默认为CPU核心数
        target_spacing: 目标体素间距
    """
    # 收集所有要处理的文件
    all_seg_files = []

    print("收集所有分割文件...")
    for center in os.listdir(root_dir):
        if seg_str in center:
            center_dir = os.path.join(root_dir, center)
            seg_files = list_file_tree(center_dir, 'nii.gz')
            seg_files = [cc for cc in seg_files if 'cor' not in cc.lower()]
            all_seg_files.extend(seg_files)

    print(f"找到 {len(all_seg_files)} 个分割文件需要处理")

    if len(all_seg_files) == 0:
        print("没有找到需要处理的文件")
        return

    # 设置进程数
    if num_processes is None:
        num_processes = multiprocessing.cpu_count()

    print(f"使用 {num_processes} 个进程并行处理")

    # 创建共享变量用于跟踪进度
    manager = multiprocessing.Manager()
    processed_files = manager.Value('i', 0)
    lock = manager.Lock()

    # 创建进程池
    with multiprocessing.Pool(processes=num_processes) as pool:
        # 为每个文件创建处理任务
        tasks = []
        for seg_path in all_seg_files:
            tasks.append((seg_path, root_dir, output_base_dir, target_spacing))

        # 使用partial函数固定回调函数的参数
        callback_with_params = partial(progress_callback,
                                       total_files=len(all_seg_files),
                                       processed_files=processed_files,
                                       lock=lock)

        # 并行处理
        print("开始并行处理...")
        results = []

        # 使用imap_unordered以获取更好的性能
        for result in pool.starmap(process_single_file, tasks):
            results.append(result)
            callback_with_params(result)

        # 统计结果
        success_count = sum(1 for r in results if r['status'] == 'success')
        warning_count = sum(1 for r in results if r['status'] == 'warning')
        error_count = sum(1 for r in results if r['status'] == 'error')

        print("\n" + "=" * 50)
        print(f"处理完成!")
        print(f"成功: {success_count} 个文件")
        print(f"警告: {warning_count} 个文件 (无分割区域)")
        print(f"错误: {error_count} 个文件")
        print("=" * 50)

        # 如果有错误，输出错误文件列表
        if error_count > 0:
            print("\n错误文件列表:")
            for r in results:
                if r['status'] == 'error':
                    print(f"  - {r['file']}: {r['message']}")

        return results


# 串行处理函数（备用，用于调试）
def process_files_serial(root_dir, output_base_dir, target_spacing=(1.0, 1.0, 1.0)):
    """
    串行处理所有文件（用于调试或特殊情况）
    """
    all_seg_files = []

    print("收集所有分割文件...")
    for center in os.listdir(root_dir):
        if seg_str in center:
            center_dir = os.path.join(root_dir, center)
            seg_files = list_file_tree(center_dir, 'nii.gz')
            seg_files = [cc for cc in seg_files if 'cor' not in cc.lower()]
            all_seg_files.extend(seg_files)

    print(f"找到 {len(all_seg_files)} 个分割文件需要处理")

    results = []
    for i, seg_path in enumerate(all_seg_files):
        print(f"\n[{i + 1}/{len(all_seg_files)}] ", end="")
        result = process_single_file(seg_path, root_dir, output_base_dir, target_spacing)
        results.append(result)

    return results


# ============================ 新增：文件监控与自动处理功能 ============================
import time
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import threading
from queue import Queue
from pathlib import Path
from datetime import datetime, timedelta


class NiiFileHandler(FileSystemEventHandler):
    """
    监控 .nii.gz 文件创建事件的处理类
    """

    def __init__(self, root_dir, output_base_dir, target_spacing=(1.0, 1.0, 1.0),
                 process_queue=None, delay_seconds=10):
        """
        初始化处理器
        Args:
            root_dir: 根监控目录（即原始代码中的root_dir）
            output_base_dir: 输出基础目录
            target_spacing: 目标体素间距
            process_queue: 处理队列，用于线程间通信
            delay_seconds: 延迟处理的秒数，默认10秒
        """
        self.root_dir = root_dir
        self.output_base_dir = output_base_dir
        self.target_spacing = target_spacing
        self.process_queue = process_queue
        self.delay_seconds = delay_seconds
        # 用于存储文件检测时间和延迟处理定时器
        self.file_detection_times = {}
        self.file_timers = {}
        print(f"[监控器] 初始化完成，监控根目录: {root_dir}")
        print(f"[监控器] 延迟处理时间: {delay_seconds}秒")

    def on_created(self, event):
        """
        当有新文件创建时触发
        """
        if not event.is_directory:
            file_path = event.src_path
            # 只处理 .nii.gz 文件，并且路径中包含"重命名SEG"
            if file_path.endswith('.nii.gz') and seg_str in file_path:
                # 忽略可能临时文件，或确保文件已完全写入（可选）
                if '_cor' in file_path.lower():
                    print(f"[监控器] 忽略疑似冠状面文件: {file_path}")
                    return

                current_time = datetime.now()
                self.file_detection_times[file_path] = current_time
                print(f"[监控器] 检测到新分割文件: {file_path} 于 {current_time.strftime('%H:%M:%S')}")
                print(f"[监控器] 将在 {self.delay_seconds} 秒后处理此文件...")

                # 设置延迟处理定时器
                timer = threading.Timer(self.delay_seconds, self._delayed_process, args=[file_path])
                timer.daemon = True
                timer.start()
                self.file_timers[file_path] = timer

    def on_modified(self, event):
        """
        当文件被修改时触发，用于检测文件可能还在写入中
        """
        if not event.is_directory:
            file_path = event.src_path
            if file_path.endswith('.nii.gz') and seg_str in file_path and file_path in self.file_detection_times:
                # 如果文件在延迟期间被修改，重新计算延迟时间
                current_time = datetime.now()
                original_time = self.file_detection_times[file_path]

                # 如果文件在最近几秒内被修改，重置定时器
                if (current_time - original_time).seconds < self.delay_seconds:
                    # 取消现有定时器
                    if file_path in self.file_timers:
                        self.file_timers[file_path].cancel()

                    # 重新设置定时器
                    remaining_delay = self.delay_seconds - (current_time - original_time).seconds
                    timer = threading.Timer(remaining_delay, self._delayed_process, args=[file_path])
                    timer.daemon = True
                    timer.start()
                    self.file_timers[file_path] = timer
                    print(f"[监控器] 文件 {file_path} 正在写入，重置延迟时间为 {remaining_delay} 秒后")

    def on_deleted(self, event):
        """
        当文件被删除时触发，清理相关资源
        """
        if not event.is_directory:
            file_path = event.src_path
            if file_path in self.file_timers:
                self.file_timers[file_path].cancel()
                del self.file_timers[file_path]
                if file_path in self.file_detection_times:
                    del self.file_detection_times[file_path]
                print(f"[监控器] 文件 {file_path} 被删除，取消处理任务")

    def _delayed_process(self, file_path):
        """
        延迟处理文件的实际函数
        """
        # 检查文件是否仍然存在
        if not os.path.exists(file_path):
            print(f"[监控器] 文件 {file_path} 不存在，跳过处理")
            if file_path in self.file_timers:
                del self.file_timers[file_path]
            if file_path in self.file_detection_times:
                del self.file_detection_times[file_path]
            return

        # 检查文件是否可读（避免文件被其他进程占用）
        try:
            with open(file_path, 'rb') as f:
                # 尝试读取文件开头，检查是否可访问
                f.read(1)
        except IOError as e:
            print(f"[监控器] 文件 {file_path} 无法访问 ({e})，等待额外5秒后重试")
            # 如果文件不可访问，再等待5秒
            timer = threading.Timer(5, self._delayed_process, args=[file_path])
            timer.daemon = True
            timer.start()
            self.file_timers[file_path] = timer
            return

        # 检查文件最近修改时间，确保文件已稳定
        try:
            file_stat = os.stat(file_path)
            current_time = time.time()
            file_mtime = file_stat.st_mtime

            # 如果文件在最近2秒内被修改，再等待2秒
            if current_time - file_mtime < 2:
                print(f"[监控器] 文件 {file_path} 最近被修改，等待额外2秒")
                timer = threading.Timer(2, self._delayed_process, args=[file_path])
                timer.daemon = True
                timer.start()
                self.file_timers[file_path] = timer
                return
        except OSError as e:
            print(f"[监控器] 无法获取文件状态 {file_path}: {e}")

        print(f"[监控器] 开始处理延迟任务: {file_path}")
        detection_time = self.file_detection_times.get(file_path, datetime.now())
        delay_actual = (datetime.now() - detection_time).seconds
        print(f"[监控器] 实际延迟时间: {delay_actual}秒")

        # 清理计时器记录
        if file_path in self.file_timers:
            del self.file_timers[file_path]
        if file_path in self.file_detection_times:
            del self.file_detection_times[file_path]

        # 将文件路径放入队列，由工作线程处理
        if self.process_queue is not None:
            self.process_queue.put(file_path)
        else:
            # 如果没有队列，则直接处理
            self._process_file_directly(file_path)

    def _process_file_directly(self, seg_path):
        """直接处理文件（简单示例，实际建议使用队列+工作线程模式）"""
        print(f"[监控器] 开始直接处理文件: {seg_path}")
        result = process_single_file(seg_path, self.root_dir, self.output_base_dir, self.target_spacing)
        print(f"[监控器] 文件处理结果: {result['status']} - {result['message']}")


def file_processing_worker(process_queue, root_dir, output_base_dir, target_spacing):
    """
    文件处理工作线程函数，从队列中取出文件路径并进行处理
    """
    print("[工作线程] 文件处理线程已启动。")
    while True:
        file_path = process_queue.get()  # 阻塞直到获取到新任务
        if file_path is None:  # 接收到终止信号
            print("[工作线程] 接收到终止信号，线程退出。")
            break

        print(f"[工作线程] 从队列获取任务: {file_path}")
        result = process_single_file(file_path, root_dir, output_base_dir, target_spacing)
        print(f"[工作线程] 处理完成: {result['status']} - {result['message']}")
        process_queue.task_done()


def start_file_monitoring(root_dir, output_base_dir, target_spacing=(1.0, 1.0, 1.0), delay_seconds=10):
    """
    启动文件监控服务
    Args:
        root_dir, output_base_dir, target_spacing: 同主处理函数
        delay_seconds: 延迟处理的秒数，默认10秒
    """
    # 创建处理队列和工作线程
    process_queue = Queue()
    worker_thread = threading.Thread(target=file_processing_worker,
                                     args=(process_queue, root_dir, output_base_dir, target_spacing),
                                     daemon=True)
    worker_thread.start()

    # 设置监控
    event_handler = NiiFileHandler(root_dir, output_base_dir, target_spacing,
                                   process_queue, delay_seconds)
    observer = Observer()

    # 监控根目录下所有名称包含"重命名SEG"的子目录
    monitored_paths = []
    for item in Path(root_dir).iterdir():
        if item.is_dir() and seg_str in item.name:
            monitored_paths.append(str(item))
            print(f"[监控器] 将监控目录: {item}")

    if not monitored_paths:
        print(f"[监控器] 错误: 在 {root_dir} 下未找到任何包含seg_str的目录。监控已终止。")
        return

    for path in monitored_paths:
        observer.schedule(event_handler, path, recursive=True)  # recursive=True 监控子目录

    observer.start()
    print(f"[监控器] 文件监控服务已启动。延迟处理时间: {delay_seconds}秒。按 Ctrl+C 停止。")

    try:
        while True:
            time.sleep(1)  # 保持主线程运行
    except KeyboardInterrupt:
        print("\n[监控器] 接收到中断信号，正在停止监控...")
        observer.stop()
        # 取消所有正在等待的定时器
        for file_path, timer in list(event_handler.file_timers.items()):
            timer.cancel()
            print(f"[监控器] 取消文件 {file_path} 的延迟处理")
        # 可选：发送终止信号给工作线程
        # process_queue.put(None)
    finally:
        observer.join()
        print("[监控器] 文件监控服务已停止。")


# ============================ 新增功能代码结束 ============================
# 主程序入口
if __name__ == "__main__":
    # 设置路径和参数
    root_dir = '/home/khtao/khtao_home/Dataset/risk_dataset'
    output_base_dir = '/home/khtao/khtao_home/Dataset/risk_dataset/processed_slices_nnUNetv2_resenc0.708'
    target_spacing = (1.0, 1.0, 1.0)

    # 运行模式选择
    # 可选模式: "batch" (批量处理), "monitor" (监控模式)
    run_mode = "monitor"  # 请在此处设置您想要的模式

    # 新增：延迟处理时间设置（仅监控模式有效）
    delay_seconds = 10  # 延迟10秒处理，防止IO错误

    if run_mode == "batch":
        # 原有的批量处理模式
        use_parallel = True  # 设置为False使用串行处理（用于调试）
        if use_parallel:
            process_files_in_parallel(root_dir, output_base_dir, num_processes=None, target_spacing=target_spacing)
        else:
            process_files_serial(root_dir, output_base_dir, target_spacing=target_spacing)

    elif run_mode == "monitor":
        # 新增的监控模式
        # 注意：运行此模式需要先安装 watchdog 库: pip install watchdog
        try:
            from watchdog.observers import Observer

            # 启动监控服务，传入延迟时间参数
            start_file_monitoring(root_dir, output_base_dir, target_spacing, delay_seconds)
        except ImportError:
            print("错误：未找到 'watchdog' 库。监控模式需要额外安装。")
            print("请运行: pip install watchdog")

    else:
        print(f"错误：未知的运行模式 '{run_mode}'。请设置为 'batch' 或 'monitor'。")
