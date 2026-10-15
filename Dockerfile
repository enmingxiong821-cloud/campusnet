# 校园网自连 · 容器镜像
#
# 运行时是纯标准库，所以这里没有 pip install 那一层：镜像里除了 CPython
# 就只有仓库文件。没有第三方依赖可被投毒，也没有「锁文件过期」这回事。
#
# 构建（在仓库根执行，上下文必须是根目录）：
#   docker build -t campusnet .
# 或者直接 docker compose up -d

FROM python:3.12-slim

# 不写 __pycache__（容器是一次性的，留在层里只占地方）；
# 不缓冲输出（否则 docker logs 要等缓冲满才看得见）。
#
# CAMPUSNET_KEY_FILE 必须指进卷里：crypto 默认把密钥读到 ~/.campusnet_obf.key，
# 而容器里的 HOME 不在挂载点上，容器一重建密钥就没了——账号密码全变成解不开的
# 密文（数据库还在，却没东西能解），而 README 承诺的是「挂一个卷就带得走」。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUTF8=1 \
    CAMPUSNET_KEY_FILE=/app/database/.campusnet_obf.key

WORKDIR /app

# 只拷运行时真正要用的三样。paths._repo_root() 按 campusnet/paths.py 往上数
# 三层得到 /app，所以目录层级必须原样保持，不能拍平成 /app/campusnet。
COPY project/backend /app/project/backend
COPY project/frontend /app/project/frontend
COPY utils /app/utils
COPY database/schema.sql database/config.example.yml /app/database/

# 以非 root 跑。控制台是有令牌认证的网络入口，镜像里也没有任何一步需要 root：
# 认证、探活、监控、WOL 的广播包都是普通权限能做的事。
# 唯一需要特权的是远程关机，而容器里本来也关不掉宿主机——那件事交给宿主机上的
# systemd 单元（packaging/campusnet.service），不要为了它把镜像跑成 root。
#
# 对 useradd 留了退路：这行没法在本机先验证（开发机没有跑着的 Docker 守护进程），
# 而 slim 镜像是否自带 useradd 随基础镜像变。缺了就装 passwd，不让构建在这里
# 变成一个看不懂的 127。
RUN set -eux; \
    if ! command -v useradd >/dev/null 2>&1; then \
        apt-get update; \
        apt-get install -y --no-install-recommends passwd; \
        rm -rf /var/lib/apt/lists/*; \
    fi; \
    useradd --system --uid 10001 --shell /usr/sbin/nologin campusnet; \
    mkdir -p /app/database/logs; \
    chown -R campusnet:campusnet /app

USER campusnet

# 数据库、日志、控制台令牌和实例锁都落在 database/ 下（见 paths.db_path /
# logging_setup.log_dir），挂一个卷就能把账号与事件记录带出容器。
VOLUME ["/app/database"]

EXPOSE 2268

# 用 urllib 而不是 curl：slim 镜像里没有 curl，而解释器一定在。
# /healthz 是公开路由，不需要令牌，也就不会因为没配好令牌而误判成不健康。
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request as u; sys.exit(0 if u.urlopen('http://127.0.0.1:2268/healthz', timeout=3).status == 200 else 1)"]

ENTRYPOINT ["python", "utils/run.py"]

# 容器里必须监听 0.0.0.0，否则端口映射进不来。
# 令牌是唯一的门禁，所以映射到宿主机时请绑 127.0.0.1（见 docker-compose.yml），
# 别用默认的 -p 2268:2268 —— 那等于把这个入口挂到局域网上。
CMD ["serve", "--host", "0.0.0.0", "--port", "2268"]
