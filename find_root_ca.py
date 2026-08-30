"""诊断 TLS 中间人代理：告诉你要信任哪张根证书、去哪儿导出。

在**跑 MaiBot 的那台机器**上执行（用 MaiBot 同一个 Python 解释器）：

    python find_root_ca.py                      # 默认诊断 vcpedia.cn
    python find_root_ca.py --host baidu.com     # 换别的站确认是不是全局现象

它不校验证书地连一次目标站点，取出对端证书并打印「主体 / 签发者」。
签发者就是签发这张证书的 CA 名字——去 Windows 证书管理器里搜它、
导出成 Base64 X.509，路径填进 config.toml 的 crawler.ca_bundle 即可。

只读网络与本地证书信息，不改任何东西，可放心运行。
"""

import argparse
import socket
import ssl
import sys
import tempfile
from pathlib import Path

# 常见的公共根 CA 关键字；签发者命中这些说明证书没问题，不是中间人
KNOWN_PUBLIC_CA = (
    "DigiCert", "GlobalSign", "Let's Encrypt", "ISRG Root", "Sectigo",
    "GoDaddy", "GeoTrust", "Thawte", "Comodo", "Entrust", "Amazon",
    "Google Trust", "GTS Root", "Certum", "Actalis", "Baltimore",
    "Microsoft", "USERTrust", "Starfield", "TrustAsia", "WoSign",
)

WINDOWS_STEPS = """接下来的步骤（Windows）：

1. Win + R，输入 certmgr.msc 回车
2. 展开「受信任的根证书颁发机构」→「证书」
3. 点「颁发给」列排序，搜上面那个【签发者】的名字，找到那张证书
   （找不到就去「中间证书颁发机构」里也找一遍）
4. 右键 → 所有任务 → 导出 → 下一步 → 选「Base64 编码 X.509 (.CER)」
5. 存成一个文件，比如 C:/mai/proxy-root-ca.cer
6. 在插件的 config.toml 里填：

   [crawler]
   ca_bundle = "C:/mai/proxy-root-ca.cer"

7. 重启 MaiBot

提示：把 .cer 改成 .pem 也行，内容一样（都是 Base64 PEM）。
实在找不到或懒得找，也可以临时用 crawler.verify_ssl = false，
但那样会跳过证书校验，只在确认代理是你自己的时候才用。"""


def _decode_peer_cert(der: bytes) -> dict:
    """借 ssl 内部的解码器解析证书（标准库自带，省得装依赖）。

    注意它只吃 PEM，传 DER 会报 "Error decoding PEM-encoded file"。
    """
    tmp = Path(tempfile.mkdtemp(prefix="rootca_")) / "peer.pem"
    tmp.write_text(ssl.DER_cert_to_PEM_cert(der), encoding="utf-8")
    try:
        return ssl._ssl._test_decode_cert(str(tmp))
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _fmt_name(rdns) -> str:
    return ", ".join(f"{key}={value}" for rdn in (rdns or ()) for key, value in rdn)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="诊断 TLS 中间人代理，找出该信任的根证书"
    )
    parser.add_argument("--host", default="vcpedia.cn", help="要诊断的站点（默认 vcpedia.cn）")
    parser.add_argument("--port", type=int, default=443)
    parser.add_argument("--out", default="", help="顺带把对端证书存成 PEM 的路径")
    args = parser.parse_args()

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((args.host, args.port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=args.host) as tls:
                der = tls.getpeercert(binary_form=True)
    except OSError as exc:
        print(f"连不上 {args.host}:{args.port}: {exc}")
        print("先确认这台机器能不能访问该站点（DNS / 代理 / 防火墙）。")
        return 1
    if not der:
        print("没拿到对端证书。")
        return 1

    if args.out:
        pem = ssl.DER_cert_to_PEM_cert(der)
        Path(args.out).write_text(pem, encoding="utf-8")
        print(f"对端证书已存到: {args.out}")

    info = _decode_peer_cert(der)
    subject = _fmt_name(info.get("subject"))
    issuer = _fmt_name(info.get("issuer"))

    print()
    print(f"目标站点 : {args.host}:{args.port}")
    print(f"证书主体 : {subject}")
    print(f"证书签发者: {issuer}")
    print(f"有效期至 : {info.get('notAfter', '未知')}")
    print()

    if any(keyword in issuer for keyword in KNOWN_PUBLIC_CA):
        print("签发者看起来是常见的公共根 CA，证书链正常——")
        print("如果这个站点也报证书错误，多半是本机缺少根证书更新或时间不对，")
        print("不太像是中间人代理。")
        return 0

    if subject == issuer:
        print(">>> 这是一张自签证书（主体 = 签发者）。")
        print(">>> 说明你的网络出口直接用自签证书顶替了站点证书，")
        print(">>> 要信任的就是【它本身】。")
    else:
        print(">>> 这张证书由另一张 CA 签发，说明链路里多了一层。")
        print(">>> 要找的是它的【签发者】那张证书。")

    print()
    print(WINDOWS_STEPS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
