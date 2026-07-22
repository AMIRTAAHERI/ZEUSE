import re
import json
import asyncio
import httpx
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs
import base64

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.constants import ChatAction

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

@dataclass
class ServerConfig:
    """Server configuration data structure"""
    url: str
    protocol: str  # vless, vmess, ss, wireguard
    country: str
    host: str
    port: int
    is_working: bool = False
    last_check: Optional[str] = None
    ping_time: Optional[int] = None

class ServerManager:
    """Manages server data storage and retrieval"""
    
    def __init__(self, file_path: str = "servers.json"):
        self.file_path = file_path
        self.servers: Dict[str, List[ServerConfig]] = {}
        self.load_servers()
    
    def load_servers(self):
        """Load servers from JSON file"""
        try:
            with open(self.file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.servers = data
        except FileNotFoundError:
            self.servers = {}
    
    def save_servers(self):
        """Save servers to JSON file"""
        with open(self.file_path, 'w', encoding='utf-8') as f:
            json.dump(self.servers, f, ensure_ascii=False, indent=2)
    
    def add_server(self, protocol: str, config: ServerConfig):
        """Add a new server"""
        if protocol not in self.servers:
            self.servers[protocol] = []
        
        # Convert to dict for JSON serialization
        server_dict = {
            'url': config.url,
            'protocol': config.protocol,
            'country': config.country,
            'host': config.host,
            'port': config.port,
            'is_working': config.is_working,
            'last_check': config.last_check,
            'ping_time': config.ping_time
        }
        
        self.servers[protocol].append(server_dict)
        self.save_servers()
    
    def get_servers_by_country(self, country: str, protocol: Optional[str] = None) -> List[Dict]:
        """Get servers filtered by country and optional protocol"""
        result = []
        
        for proto, servers in self.servers.items():
            if protocol and proto != protocol:
                continue
            
            for server in servers:
                if server['country'].lower() == country.lower():
                    result.append(server)
        
        return result
    
    def get_all_countries(self) -> set:
        """Get all available countries"""
        countries = set()
        for servers in self.servers.values():
            for server in servers:
                if server['country']:
                    countries.add(server['country'])
        return sorted(countries)
    
    def get_all_protocols(self) -> set:
        """Get all available protocols"""
        return set(self.servers.keys())

class ServerParser:
    """Parse server URLs and extract configuration"""
    
    @staticmethod
    def extract_country(url: str) -> str:
        """Extract country from server URL"""
        # Try to find country in URL fragment
        if '#' in url:
            fragment = url.split('#')[1]
            # Decode URL-encoded country names
            country = fragment.replace('%2B', '+').replace('+', ' ')
            return country.strip() or 'Unknown'
        return 'Unknown'
    
    @staticmethod
    def get_protocol(url: str) -> str:
        """Determine protocol from URL"""
        if url.startswith('vless://'):
            return 'vless'
        elif url.startswith('vmess://'):
            return 'vmess'
        elif url.startswith('ss://'):
            return 'ss'
        elif url.startswith('wireguard://'):
            return 'wireguard'
        else:
            return 'unknown'
    
    @staticmethod
    def extract_host_port(url: str) -> Tuple[str, int]:
        """Extract host and port from server URL"""
        try:
            if url.startswith('vless://'):
                # Format: vless://uuid@host:port?params
                match = re.search(r'@([^:/?]+):(\d+)', url)
                if match:
                    return match.group(1), int(match.group(2))
            elif url.startswith('ss://'):
                # Format: ss://encoded@host:port
                match = re.search(r'@([^:/?#]+):(\d+)', url)
                if match:
                    return match.group(1), int(match.group(2))
            elif url.startswith('vmess://'):
                # Decode and extract from JSON
                match = re.search(r'vmess://(.+)$', url)
                if match:
                    try:
                        data = json.loads(base64.b64decode(match.group(1)).decode())
                        return data.get('add', 'unknown'), data.get('port', 443)
                    except:
                        pass
        except Exception as e:
            logger.error(f"Error parsing URL: {e}")
        
        return 'unknown', 443

class PingTester:
    """Test server connectivity and latency"""
    
    @staticmethod
    async def test_ping(host: str, port: int, timeout: int = 5) -> Optional[int]:
        """Test ping to server and return latency in ms"""
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                start_time = datetime.now()
                response = await client.get(f'http://{host}:{port}/', follow_redirects=False)
                end_time = datetime.now()
                
                if response.status_code < 500:
                    latency = int((end_time - start_time).total_seconds() * 1000)
                    return latency
        except asyncio.TimeoutError:
            return None
        except Exception as e:
            logger.debug(f"Ping test failed for {host}:{port}: {e}")
            return None
        
        return None

class TelegramBot:
    """Main Telegram Bot Class"""
    
    def __init__(self, token: str):
        self.token = token
        self.manager = ServerManager()
        self.parser = ServerParser()
        self.pinger = PingTester()
        self.user_countries: Dict[int, str] = {}
        self.user_protocols: Dict[int, str] = {}
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        keyboard = [
            [InlineKeyboardButton("➕ اضافه کردن سرور", callback_data='add_server')],
            [InlineKeyboardButton("🔍 جستجوی سرور", callback_data='search_server')],
            [InlineKeyboardButton("🌍 سرورهای کشور", callback_data='country_filter')],
            [InlineKeyboardButton("📊 آمار سرورها", callback_data='stats')],
            [InlineKeyboardButton("🔄 به‌روز رسانی", callback_data='update_servers')],
            [InlineKeyboardButton("⚙️ تنظیمات", callback_data='settings')]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        welcome_text = """
🤖 **خوش‌آمدید به ربات مدیریت سرور**

این ربات امکانات زیر را فراهم می‌کند:
✅ اضافه کردن سرورهای VLESS، VMESS، SS، Wireguard
✅ تفکیک سرورها بر اساس کشور
✅ تست خودکار ping هر 30 دقیقه
✅ نمایش سرورهای فعال برای هر کشور
✅ ذخیره‌سازی در مخزن GitHub

برای شروع، گزینه مورد نظر را انتخاب کنید:
        """
        
        await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode='Markdown')
    
    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle callback queries"""
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        
        if query.data == 'add_server':
            await self.add_server_menu(query, context)
        elif query.data == 'search_server':
            await self.search_server_menu(query, context)
        elif query.data == 'country_filter':
            await self.country_filter_menu(query, context)
        elif query.data == 'stats':
            await self.show_stats(query, context)
        elif query.data == 'update_servers':
            await self.update_servers(query, context)
        elif query.data == 'settings':
            await self.settings_menu(query, context)
        elif query.data.startswith('protocol_'):
            protocol = query.data.split('_')[1]
            self.user_protocols[user_id] = protocol
            await query.edit_message_text(
                text=f"✅ پروتکل {protocol} انتخاب شد.\n\nلطفاً سرور را به صورت زیر ارسال کنید:\n\n{protocol}://...",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data='add_server')]])
            )
        elif query.data.startswith('country_'):
            country = query.data.split('_', 1)[1]
            await self.show_country_servers(query, context, country)
        elif query.data == 'back_main':
            await self.start(update, context)
    
    async def add_server_menu(self, query, context: ContextTypes.DEFAULT_TYPE):
        """Show protocol selection for adding server"""
        keyboard = [
            [InlineKeyboardButton("VLESS", callback_data='protocol_vless')],
            [InlineKeyboardButton("VMESS", callback_data='protocol_vmess')],
            [InlineKeyboardButton("Shadowsocks", callback_data='protocol_ss')],
            [InlineKeyboardButton("WireGuard", callback_data='protocol_wireguard')],
            [InlineKeyboardButton("⬅️ بازگشت", callback_data='back_main')]
        ]
        
        await query.edit_message_text(
            text="🔗 لطفاً پروتکل سرور را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    async def handle_server_url(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle server URL submission"""
        user_id = update.effective_user.id
        url = update.message.text.strip()
        
        if user_id not in self.user_protocols:
            await update.message.reply_text("❌ لطفاً ابتدا پروتکل را انتخاب کنید.")
            return
        
        protocol = self.user_protocols[user_id]
        
        # Parse server details
        country = self.parser.extract_country(url)
        host, port = self.parser.extract_host_port(url)
        
        # Test ping
        await update.message.chat.send_action(ChatAction.TYPING)
        ping_time = await self.pinger.test_ping(host, port)
        
        # Create server config
        server = ServerConfig(
            url=url,
            protocol=protocol,
            country=country,
            host=host,
            port=port,
            is_working=ping_time is not None,
            last_check=datetime.now().isoformat(),
            ping_time=ping_time
        )
        
        # Add to manager
        self.manager.add_server(protocol, server)
        
        status = "✅" if server.is_working else "❌"
        ping_text = f"Ping: {ping_time}ms" if ping_time else "عدم اتصال"
        
        await update.message.reply_text(
            f"{status} سرور اضافه شد!\n\n"
            f"📍 کشور: {country}\n"
            f"🔗 پروتکل: {protocol}\n"
            f"🖥️ هاست: {host}:{port}\n"
            f"⏱️ {ping_text}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data='back_main')]])
        )
        
        del self.user_protocols[user_id]
    
    async def search_server_menu(self, query, context: ContextTypes.DEFAULT_TYPE):
        """Show search options"""
        keyboard = [
            [InlineKeyboardButton("🌍 جستجو بر اساس کشور", callback_data='country_filter')],
            [InlineKeyboardButton("🔗 جستجو بر اساس پروتکل", callback_data='search_protocol')],
            [InlineKeyboardButton("✅ سرورهای فعال", callback_data='active_servers')],
            [InlineKeyboardButton("⬅️ بازگشت", callback_data='back_main')]
        ]
        
        await query.edit_message_text(
            text="🔍 بر چه اساسی می‌خواهید جستجو کنید؟",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    async def country_filter_menu(self, query, context: ContextTypes.DEFAULT_TYPE):
        """Show available countries"""
        countries = list(self.manager.get_all_countries())
        
        if not countries:
            await query.edit_message_text(
                text="هیچ سروری ثبت نشده است.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data='back_main')]])
            )
            return
        
        keyboard = []
        for country in countries[:15]:  # Show first 15
            keyboard.append([InlineKeyboardButton(f"🌍 {country}", callback_data=f"country_{country}")])
        
        keyboard.append([InlineKeyboardButton("⬅️ بازگشت", callback_data='back_main')])
        
        await query.edit_message_text(
            text="🌍 کشور مورد نظر را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    async def show_country_servers(self, query, context: ContextTypes.DEFAULT_TYPE, country: str):
        """Show servers for specific country"""
        servers = self.manager.get_servers_by_country(country)
        
        if not servers:
            await query.edit_message_text(
                text=f"سرور برای کشور {country} یافت نشد.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data='country_filter')]])
            )
            return
        
        # Group by protocol
        by_protocol = {}
        for server in servers:
            protocol = server['protocol']
            if protocol not in by_protocol:
                by_protocol[protocol] = []
            by_protocol[protocol].append(server)
        
        message = f"🌍 سرورهای **{country}**\n\n"
        
        for protocol, proto_servers in by_protocol.items():
            message += f"**{protocol.upper()}** ({len(proto_servers)})\n"
            for i, server in enumerate(proto_servers[:5], 1):  # Show first 5
                status = "✅" if server['is_working'] else "❌"
                ping = f"({server['ping_time']}ms)" if server['ping_time'] else ""
                message += f"{i}. {status} {ping}\n"
            
            if len(proto_servers) > 5:
                message += f"... و {len(proto_servers) - 5} سرور دیگر\n"
            message += "\n"
        
        keyboard = [
            [InlineKeyboardButton(f"📋 {protocol.upper()}", callback_data=f'list_{country}_{protocol}')]
            for protocol in by_protocol.keys()
        ]
        keyboard.append([InlineKeyboardButton("⬅️ بازگشت", callback_data='country_filter')])
        
        await query.edit_message_text(
            text=message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    
    async def show_stats(self, query, context: ContextTypes.DEFAULT_TYPE):
        """Show server statistics"""
        total = 0
        by_protocol = {}
        by_country = {}
        working = 0
        
        for protocol, servers in self.manager.servers.items():
            by_protocol[protocol] = len(servers)
            total += len(servers)
            
            for server in servers:
                country = server['country']
                if country not in by_country:
                    by_country[country] = 0
                by_country[country] += 1
                
                if server['is_working']:
                    working += 1
        
        message = "📊 **آمار سرورها**\n\n"
        message += f"📈 کل سرورها: {total}\n"
        message += f"✅ سرورهای فعال: {working}\n"
        message += f"❌ سرورهای غیرفعال: {total - working}\n\n"
        
        message += "**پروتکل‌ها:**\n"
        for protocol, count in sorted(by_protocol.items(), key=lambda x: x[1], reverse=True):
            message += f"  🔗 {protocol}: {count}\n"
        
        message += "\n**کشورها:**\n"
        for country, count in sorted(by_country.items(), key=lambda x: x[1], reverse=True)[:10]:
            message += f"  🌍 {country}: {count}\n"
        
        keyboard = [[InlineKeyboardButton("⬅️ بازگشت", callback_data='back_main')]]
        
        await query.edit_message_text(
            text=message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    
    async def update_servers(self, query, context: ContextTypes.DEFAULT_TYPE):
        """Update server status with ping tests"""
        await query.edit_message_text(text="⏳ در حال به‌روز رسانی سرورها...")
        
        updated = 0
        working = 0
        
        for protocol in list(self.manager.servers.keys()):
            for server in self.manager.servers[protocol]:
                ping = await self.pinger.test_ping(server['host'], server['port'])
                
                server['last_check'] = datetime.now().isoformat()
                server['is_working'] = ping is not None
                server['ping_time'] = ping
                
                updated += 1
                if ping is not None:
                    working += 1
        
        self.manager.save_servers()
        
        await query.edit_message_text(
            text=f"✅ به‌روز رسانی تکمیل شد!\n\n"
            f"📊 سرورهای بررسی شده: {updated}\n"
            f"✅ سرورهای فعال: {working}\n"
            f"❌ سرورهای غیرفعال: {updated - working}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data='back_main')]])
        )
    
    async def settings_menu(self, query, context: ContextTypes.DEFAULT_TYPE):
        """Show settings menu"""
        keyboard = [
            [InlineKeyboardButton("🔄 برنامه‌ریزی خودکار", callback_data='auto_schedule')],
            [InlineKeyboardButton("🎨 تنظیمات نمایش", callback_data='display_settings')],
            [InlineKeyboardButton("💾 صادرات داده‌ها", callback_data='export_data')],
            [InlineKeyboardButton("🗑️ حذف تمام سرورها", callback_data='delete_all')],
            [InlineKeyboardButton("⬅️ بازگشت", callback_data='back_main')]
        ]
        
        await query.edit_message_text(
            text="⚙️ تنظیمات",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    async def auto_update_task(self, context: ContextTypes.DEFAULT_TYPE):
        """Periodic task to update servers every 30 minutes"""
        try:
            for protocol in list(self.manager.servers.keys()):
                for server in self.manager.servers[protocol]:
                    ping = await self.pinger.test_ping(server['host'], server['port'])
                    
                    server['last_check'] = datetime.now().isoformat()
                    server['is_working'] = ping is not None
                    server['ping_time'] = ping
            
            self.manager.save_servers()
            logger.info("Auto-update completed successfully")
        except Exception as e:
            logger.error(f"Auto-update failed: {e}")
    
    def run(self):
        """Run the bot"""
        app = Application.builder().token(self.token).build()
        
        # Add handlers
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CallbackQueryHandler(self.handle_callback))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_server_url))
        
        # Add periodic job for auto-update every 30 minutes
        job_queue = app.job_queue
        job_queue.run_repeating(self.auto_update_task, interval=1800, first=1800)  # 30 minutes
        
        # Start the bot
        app.run_polling()


if __name__ == '__main__':
    import os
    
    TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'YOUR_TOKEN_HERE')
    
    bot = TelegramBot(TOKEN)
    bot.run()
