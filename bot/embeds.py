import discord
import datetime

def create_dashboard_embed(server_name, server_ip, server_port, status, players, cpu_percent, ram_display, last_backup, last_restart, version, color):
    if status == "ONLINE":
        version_str = version
        if version_str and not version_str.lower().startswith('v'):
            version_str = f"v{version_str}"
        embed_title = f"🟢 {server_name} ({version_str})"
    elif status == "STARTING":
        embed_title = f"🟡 {server_name} (로딩 중...)"
    elif status == "UNHEALTHY":
        embed_title = f"⚠️ {server_name} (이상 발생)"
    else:
        embed_title = f"🔴 {server_name}"

    embed = discord.Embed(title=embed_title, color=color, timestamp=datetime.datetime.now())
    embed.add_field(name="상태", value=f"```\n{status}\n```", inline=True)
    embed.add_field(name="접속자", value=f"```\n{players}\n```", inline=True)
    embed.add_field(name="접속 주소", value=f"```\n{server_ip}:{server_port}\n```", inline=False)
    embed.add_field(name="CPU / RAM", value=f"```\n{cpu_percent}% / {ram_display}\n```", inline=False)
    embed.add_field(name="최근 백업", value=f"```\n{last_backup}\n```", inline=False)
    embed.add_field(name="마지막 재시작", value=f"```\n{last_restart}\n```", inline=False)
    embed.set_footer(text="1분마다 자동 갱신 (유저 변동 시 즉시 갱신)")
    
    return embed