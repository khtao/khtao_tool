import os
from os.path import join
import SimpleITK as sitk
from glob import glob
import os
import subprocess


def convert_with_subprocess(dicom_dir, output_dir):
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # 构建 dcm2niix 命令行指令
    # -z y: 压缩为 .nii.gz
    # -b y: 导出 BIDS JSON 侧车文件（保留元数据）
    # -f %p_%s: 自定义命名规则（协议名_序列号），避免文件名冲突
    command = [
        "dcm2niix",
        "-z", "y",
        "-b", "y",
        "-f", "%p_%s",
        "-o", output_dir,
        dicom_dir
    ]

    print(f"正在执行命令: {' '.join(command)}")

    # 执行命令并捕获输出
    result = subprocess.run(command, capture_output=True, text=True)

    if result.returncode == 0:
        print("转换成功！")
        print(result.stdout)
    else:
        print("转换失败，错误信息：")
        print(result.stderr)


def list_dcm(path):
    dcm_list = list()
    dir_list = os.listdir(path)
    if os.path.isdir(path):
        if len(glob(os.path.join(path, "*.DCM"))) > 1 or len(glob(os.path.join(path, "*.dcm"))) > 1 or len(
                glob(os.path.join(path, "*.IMA"))) > 1:
            dcm_list += [path]
    for dir_name in dir_list:
        sub_path = os.path.join(path, dir_name)
        if os.path.isdir(sub_path):
            dcm_list += list_dcm(sub_path)
    return dcm_list


def convert_all(dcm_path):
    files = list_dcm(dcm_path)
    for filename in files:
        convert_with_subprocess(filename, filename)


def get_file_name(filename, file_type):
    if os.path.exists(filename + file_type):
        i = 1
        while os.path.exists(filename + '_' + str(i) + file_type):
            i += 1
        new_name = filename + '_' + str(i)
    else:
        new_name = filename
    return new_name


def convert_and_save2(dcm_path, save_root):
    for pp in os.listdir(dcm_path):
        file_root = os.path.join(dcm_path, pp)
        if os.path.exists(file_root) and os.path.isdir(file_root):
            dcm_list = list_dcm(file_root)
            for dcm in dcm_list:
                if 'UnknownPatientName' in dcm:
                    continue
                save_path = os.path.join(save_root, pp, os.path.basename(dcm))
                print(dcm, save_path)
                if os.path.exists(save_path):
                    continue
                try:
                    convert_with_subprocess(dcm, save_path)
                except:
                    print('error')


def convert_and_save3(dcm_path, save_root):
    for pp in os.listdir(dcm_path):
        file_root = os.path.join(dcm_path, pp)
        if os.path.exists(file_root) and os.path.isdir(file_root):
            for cc in os.listdir(file_root):
                file_root2 = os.path.join(file_root, cc)
                if os.path.isdir(file_root2):
                    dcm_list = list_dcm(file_root2)
                    for dcm in dcm_list:
                        if 'UnknownPatientName' in dcm:
                            continue
                        save_path = os.path.join(save_root, pp, cc, os.path.basename(dcm))
                        print(dcm, save_path)
                        if os.path.exists(save_path + '.nii.gz'):
                            continue
                        try:
                            convert_with_subprocess(dcm, save_path + '.nii.gz')
                        except:
                            print('error')


if __name__ == '__main__':
    convert_all('/home/khtao/Dataset/tcga_mri')
    # convert_and_save3('/home/khtao/CRC/risk_dataset/zhong_shan_er_yuan',
    #                   '/home/khtao/WorkCenter/PycharmProjects/风险分层论文修订/radiomics_dataset/zhong_shan_er_yuan_nii2')
    # convert_and_save3('/home/khtao/CRC/risk_dataset/shantou', '/home/khtao/WorkCenter/PycharmProjects/风险分层论文修订/radiomics_dataset/shantou_nii2')
    # convert_and_save3('/home/khtao/CRC/risk_dataset/zhongshan', '/home/khtao/WorkCenter/PycharmProjects/风险分层论文修订/radiomics_dataset/zhongshan_nii2')
    #
    # convert_and_save2(r'/home/khtao/WholeSlide/子宫内膜癌_深汕妇科',
    #                   save_root=r'/home/khtao/WorkCenter/PycharmProjects/风险分层论文修订/radiomics_dataset/子宫内膜癌_深汕妇科nii')
    # convert_and_save2(r'/home/khtao/Dataset/risk_dataset/佛山市一妇科/佛山市一妇科2024-07-10',
    #                   save_root=r'/home/khtao/WorkCenter/PycharmProjects/风险分层论文修订/radiomics_dataset/佛山市一妇科nii')
    # rename_and_convert(r'G:\risk_dataset\zhongshan', r'E:\risk_dataset\shantou_nii')
