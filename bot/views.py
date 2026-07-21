import discord

class ServerControlView(discord.ui.View):
    def __init__(self, update_available=False):
        super().__init__(timeout=None)
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id == "btn_update":
                if update_available:
                    child.disabled = False
                    child.style = discord.ButtonStyle.green
                    child.label = "서버 업데이트 수동 실행"
                else:
                    child.disabled = True
                    child.style = discord.ButtonStyle.secondary
                    child.label = "최신 버전 (업데이트 없음)"

    @discord.ui.button(label="서버 업데이트", style=discord.ButtonStyle.secondary, custom_id="btn_update")
    async def update_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in interaction.client.config.admin_ids:
            return await interaction.response.send_message("❌ 이 버튼은 지정된 관리자만 사용할 수 있습니다.", ephemeral=True)
        
        await interaction.response.send_message("🚀 수동 점검 및 업데이트 시퀀스를 가동합니다. (5분 카운트다운 시작)", ephemeral=True)
        await interaction.client.run_maintenance_sequence()


class ConfigConfirmView(discord.ui.View):
    def __init__(self, changes, user_id):
        super().__init__(timeout=60)
        self.changes = changes
        self.user_id = user_id
        self.confirmed = False

    @discord.ui.button(label="변경 승인 및 재시작", style=discord.ButtonStyle.green, custom_id="btn_confirm_config")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ 명령어를 입력한 관리자만 승인할 수 있습니다.", ephemeral=True)
        self.confirmed = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="변경 취소", style=discord.ButtonStyle.red, custom_id="btn_cancel_config")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ 명령어를 입력한 관리자만 취소할 수 있습니다.", ephemeral=True)
        await interaction.response.send_message("❌ 설정 변경 취소됨. 파일이 수정되지 않았습니다.", ephemeral=True)
        self.stop()