# UOS 20 ARM64 可运行二进制打包经验

> 本文沉淀自 KeymouseGo 项目（PySide6 + PyInstaller）在 UOS 20 / 内网环境上的踩坑全过程。
> 以后凡是「给 UOS 20（或其它老 glibc 的 Linux）编译可运行二进制」的项目，先照本文过一遍 checklist。

## 1. 目标环境

| 项 | 值 |
|---|---|
| 系统 | 统信 UOS 20 |
| 架构 | aarch64（ARM64，飞腾 / 鲲鹏） |
| 基座 | Debian 10（buster），**glibc 2.28** |
| 网络 | 内网，不联网 |

**glibc 2.28 是贯穿全文的唯一硬约束。** 所有问题最后都归结为一句：产物里每个 `.so` 实际引用到的最高 glibc 符号版本必须 ≤ 2.28。

## 2. 核心原则（一句话）

> 决定二进制能否在目标机运行的，不是「构建机的 glibc 版本」，而是**产物里每个共享库实际引用到的最高 glibc 符号版本**。

glibc 向后兼容：在高层（新 glibc）编译、只要代码没用到新 API，符号需求就停在旧版本，低层（老 glibc）照样能加载。

```
要求的符号版本  ≤  目标机 glibc  ≤  构建机 glibc
   (2.17)          (2.28)          (2.39, 随便多新都行)
```

## 3. 三个关键概念（必读）

### 3.1 glibc 符号版本（symbol versioning）

`readelf -V libxxx.so` 或 `strings libxxx.so | grep GLIBC_2.` 能看到一个库引用的 glibc 版本符号，形如 `GLIBC_2.17`、`GLIBC_2.38`。运行时目标机的 `libc.so.6` 必须提供**大于等于**这些版本的符号。

- `libc.so.6` 由目标机系统提供，**PyInstaller 不打包它**（打包了也没用/有害）。
- 所以任何一个库引用 `GLIBC_2.38`，而目标机 libc 只有 2.28，加载就直接报：
  `version GLIBC_2.38 not found`。
- **GLIBCXX（libstdc++）不是这个约束**：`libstdc++.so.6` 可以打进 bundle，bundle 自带的 libstdc++ 会满足高 GLIBCXX，不用管目标机。

### 3.2 manylinux tag

pip 的 Linux wheel 用 `manylinux_2_X` 标注「构建环境的 glibc 版本」。它表示「保证在 glibc ≥ X 上可安装」，但也反映构建工具链的 glibc 基准。实测：

| pip 的 PySide6 | aarch64 wheel 的 manylinux tag | 实测 Qt 库 GLIBC 基线 |
|---|---|---|
| 6.5.3 / 6.6~6.8 | manylinux_2_31 | 需 glibc ≥ 2.31 |
| 6.9 / 6.10 | manylinux_2_39 | 需 glibc ≥ 2.38（libQt6Gui 实测 2.38） |

**对 glibc 2.28 的目标机，pip 的 PySide6 等于死路**（任何版本基线都 ≥2.31）。

### 3.3 conda-forge 的 sysroot（正确路线）

conda-forge 用 CentOS 7 的 sysroot（`cos7`）编译，**glibc 基线压到 2.17**，Qt、openssl、python 全都 2.17 基线。所以 conda 打包出来的库能在 glibc 2.17+ 的任何系统跑，自然覆盖 UOS 的 2.28。

而且 conda 包自带低 glibc 基线，**与构建机 host 的 glibc 无关** —— 不用为了匹配目标机而去搞 Debian 10 Docker 容器，直接在最新 runner 上跑 Miniforge 即可。

## 4. 推荐构建方案（最终验证通过）

直接 `ubuntu-24.04-arm` 原生 runner + Miniforge，**不用 Docker、不用 QEMU**：

```yaml
jobs:
  build:
    runs-on: ubuntu-24.04-arm
    steps:
      - uses: actions/checkout@v4

      - name: 安装 Miniforge (aarch64)
        run: |
          wget -q https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-aarch64.sh -O /tmp/miniforge.sh
          bash /tmp/miniforge.sh -b -p /opt/conda

      - name: 创建 conda 环境
        run: |
          /opt/conda/bin/conda create -n build python=3.10 -y
          # qt6-multimedia 是独立包，pyside6 不带它，必须显式装
          /opt/conda/bin/conda install -n build -c conda-forge "pyside6=6.10" "qt6-multimedia=6.10.*" pyinstaller -y

      - name: 装纯 Python 依赖
        run: |
          source /opt/conda/etc/profile.d/conda.sh
          conda activate build
          sed -i '/PySide6/d' requirements.txt   # PySide6 走 conda，别 pip 装
          pip install -r requirements.txt

      - name: 打包
        run: |
          source /opt/conda/etc/profile.d/conda.sh
          conda activate build
          export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:$LD_LIBRARY_PATH"
          pyinstaller -Fw \
            --add-binary "$CONDA_PREFIX/lib/libcrypto.so.3:." \
            --add-binary "$CONDA_PREFIX/lib/libssl.so.3:." \
            --add-data './assets:assets' \
            KeymouseGo.py
```

要点：
- Python 版本用 3.10（匹配已验证组合），PySide6 锁 6.10。
- `qt6-multimedia=6.10.*` 与 pyside6 版本对齐（Qt 库 ABI 必须匹配）。
- 全程在 runner 上离线不行——conda/pip 需要下载，但这是「构建机」而不是「目标机」，联网没问题。

## 5. 踩坑全景（按排查顺序）

| # | 现象 | 根因 | 解法 |
|---|---|---|---|
| 1 | `ImportError: libQt6Multimedia.so.6: cannot open shared object file` | conda 的 `pyside6` 只含 QtMultimedia 的 Python 绑定，底层 `libQt6Multimedia.so.6` 在独立包 `qt6-multimedia` 里 | `conda install qt6-multimedia=6.10.*` |
| 2 | `libpython3.10.so.1.0: version GLIBC_2.38 not found` | `setup-python` 的 Linux Python 是在新 glibc 环境编译的 | 不用 setup-python，改用 conda 的 python（2.17 基线） |
| 3 | `libQt6Gui.so.6: version GLIBC_2.38 not found` | pip 的 PySide6 manylinux_2_39 wheel，Qt 库高 glibc | 全用 conda-forge 装 PySide6（2.17 基线） |
| 4 | `libcrypto.so.3: version GLIBC_2.38 not found` | PyInstaller 通过 `_ssl`/`_hashlib` 模块收集 openssl 时走系统搜索路径，误抓系统版（2.38/2.33） | `--add-binary` 强制注入 conda 版（2.17） |

### 坑 4 详解：openssl 为什么会在包里、为什么会抓错

- **为什么在包里**：openssl 不是「联网」需求，是 Python `hashlib`（md5/sha 等）与 `ssl`（TLS）标准库的底层依赖。任何程序 `import hashlib`（哪怕间接）都会被 PyInstaller 收集进 bundle，与「离线」无关。
- **为什么抓错**：PyInstaller 收集 Qt 库时走的是专门的 PySide6 hook（认得 conda 布局），能正确拿到 conda 的 2.17 库；但收集 openssl 走的是 **`_ssl`/`_hashlib` 的通用 hook + 系统搜索路径**，直接找到了 `/usr/lib/aarch64-linux-gnu/libcrypto.so.3`（host 版 2.38）。
- **教训**：PySide6 这类大框架的 conda 布局能被 hook 正确收集，但「标准库扩展的依赖」（openssl、libffi 等）是漏网之鱼，需要单独 `--add-binary` 兜底。

### 通用教训

1. **不要用 pip 装带 C 扩展/系统库的东西**（PySide6、numpy 等），它们只有 manylinux wheel，glibc 基线太高。能用 conda 就用 conda。
2. **「没 wheel / 装不上」不代表「没有」**。PySide6 一直有 aarch64 wheel，只是要 glibc ≥2.31，老容器里 pip 装不上才会误判。
3. **老 glibc 目标机，构建机越新越好**（原生 ARM runner + conda），而不是反过来搞老容器（容器 glibc 老，conda/pip 反而装不上新 wheel）。

## 6. 产物验证方法（必做，否则到内网才炸）

打包完必须拆包 + 扫描 glibc 基线，确认没有任何库引用 > 2.28 的 GLIBC 符号。

### 6.1 拆包

```bash
# pyinstxtractor 拆 PyInstaller 单文件
curl -sL https://raw.githubusercontent.com/extremecoders-re/pyinstxtractor/master/pyinstxtractor.py -o pyinstxtractor.py
python3 pyinstxtractor.py dist/KeymouseGo   # 生成 KeymouseGo_extracted/
```

### 6.2 扫描所有 .so 的 glibc 基线（修正后的正确写法）

```python
import re, os
base = 'KeymouseGo_extracted'

def maxver(data, pat):
    vers = sorted(set(re.findall(pat, data)),
                  key=lambda v: tuple(int(x) for x in v.replace(b'GLIBC_', b'').split(b'.')))
    return vers[-1].decode() if vers else None

def key(v):  # 'GLIBC_2.17' -> (2, 17)
    v = v.replace('GLIBC_', '')
    return tuple(int(x) for x in v.split('.')) if v else (0, 0)

bad = []
for root, _, files in os.walk(base):
    for f in files:
        if '.so' not in f:
            continue
        p = os.path.join(root, f)
        data = open(p, 'rb').read()
        if data[:4] != b'\x7fELF':  # 跳过 PyInstaller 的符号链接 stub
            continue
        g = maxver(data, rb'GLIBC_2\.[0-9.]+')
        if g and key(g) > (2, 28):
            bad.append((g, os.path.relpath(p, base)))

for g, rel in sorted(bad):
    print(g, rel)
print('超标总数:', len(bad))
```

> ⚠️ **易错点**：解析版本号时要把 `GLIBC_2.17` 拆成 `(2, 17)`，别用 `split('.')[1:]` 这种会得到 `(17,)` 的写法——那会把 2.17 误判成「大于」2.28，导致满屏假警报（本次就踩过）。
> 另外注意单文件 CArchive 内部是 zlib 压缩的，直接 `strings` 原始文件搜不到 GLIBC 符号，必须**先拆包**再扫每个 .so。

### 6.3 目标机验证

运行时日志里常见「无害警告」，不要误判为错误：

| 日志 | 含义 |
|---|---|
| `Could not load the Qt platform plugin "wayland"` | 落在 X11 回退，Qt 自己处理，不影响 |
| `Fontconfig error: Cannot load default config file` | 没配字体配置，用默认字体 |
| `DeprecationWarning: AA_EnableHighDpiScaling` | PySide6 6.10 API 弃用提示 |
| `PulseAudioService: pa_context_connect() failed` | 无音频设备（sudo 环境常见） |

这些 OK 版同样会报，「程序正常初始化（走到 main thread）并能干活」才是通过标准。

## 7. 复用 checklist（以后 UOS 项目照此排查）

- [ ] 目标机 glibc 版本确定了吗？（`ldd --version`，UOS 20 = 2.28）
- [ ] UI/二进制框架（PySide6/PyQt/numpy 等）用的是 **conda-forge**，不是 pip？
- [ ] conda 的「拆分模块」（如 `qt6-multimedia`）显式装了吗？
- [ ] Python 解释器来自 conda，不是 setup-python / 系统 python？
- [ ] 标准库扩展依赖（openssl/libffi）有没有 `--add-binary` 强制指向 conda 版本？
- [ ] 打包后拆包扫过所有 .so 的 GLIBC 基线，确认 ≤ 目标机版本？
- [ ] 目标机实机跑过，确认走到正常初始化（不是只看「没报 GLIBC 错」）？

## 8. 关键版本/包名速查（本次实测）

| 项 | 值 |
|---|---|
| Miniforge aarch64 | `Miniforge3-Linux-aarch64.sh` |
| conda python | 3.10 |
| conda PySide6 | `pyside6=6.10`（含 QtCore/Gui/Widgets/Multimedia 等绑定） |
| QtMultimedia 库 | `qt6-multimedia=6.10.*`（独立包） |
| openssl | conda 的 `openssl`（随 python 依赖自动装），`$CONDA_PREFIX/lib/lib{crypto,ssl}.so.3` |
| PyInstaller | 6.x（pip 装到 conda 环境，保持较新） |
| 拆包工具 | pyinstxtractor（extremecoders-re） |