FROM continuumio/miniconda3:latest

WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Asia/Shanghai

# Build command: docker build -t gongwen-rag-system:latest .

# 使用阿里云 Debian 镜像源，加快 apt 下载速度。
RUN sed -i 's|http://deb.debian.org/debian|https://mirrors.aliyun.com/debian|g' /etc/apt/sources.list.d/debian.sources \
    && sed -i 's|http://deb.debian.org/debian-security|https://mirrors.aliyun.com/debian-security|g' /etc/apt/sources.list.d/debian.sources

# 安装系统依赖：音视频处理、公文 PDF 预览、字体管理和部分 Python 包编译所需的工具。
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libreoffice \
    fontconfig \
    unzip \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# ttf.zip 需要放在后端项目根目录，也就是 Docker 构建上下文中。
COPY ttf.zip /tmp/ttf.zip

# 安装公文预览所需的方正字体，并刷新系统字体缓存。
RUN mkdir -p /usr/share/fonts/truetype/founder \
    && unzip -o /tmp/ttf.zip -d /tmp \
    && cp /tmp/ttf/* /usr/share/fonts/truetype/founder/ \
    && fc-cache -fv \
    && rm -rf /tmp/ttf /tmp/ttf.zip

# 先复制依赖文件，方便 Docker 缓存 Python 依赖安装层。
COPY requirements.txt /app/requirements.txt

# 创建 Python 3.11 的 conda 环境，并使用阿里云 PyPI 镜像安装后端依赖。
RUN conda create -y -n gongwen2025 python=3.11 \
    && conda run -n gongwen2025 pip install --no-cache-dir \
       -r /app/requirements.txt \
       -i https://mirrors.aliyun.com/pypi/simple/ \
    && conda clean -afy

# 依赖安装完后再复制业务代码，代码变更时可以尽量复用上面的构建缓存。
COPY . /app

EXPOSE 8080

CMD ["conda", "run", "--no-capture-output", "-n", "gongwen2025", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
