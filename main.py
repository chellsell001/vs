import os
import uuid
import threading
import re
from datetime import datetime
from dotenv import load_dotenv
import telebot
from telebot import types
from sqlalchemy import create_engine, Column, String, Float, DateTime, Integer
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import requests

# Загрузка конфигурации
load_dotenv()
bot = telebot.TeleBot('8138858529:AAF5b7U3Gy_an0Af_JTuSkIJZoCT3j4qt1I')

# Константы
COUNTRIES = {
    '🇷🇺 Россия': {'code': '+7', 'length': 11, 'example': '9123456789'},
    '🇰🇿 Казахстан': {'code': '+7', 'length': 10, 'example': '7012345678'},
    '🇺🇦 Украина': {'code': '+380', 'length': 9, 'example': '501234567'}
}
SERVICES = ['📱 WhatsApp', '✈️ Telegram']
RESERVE_TIME = 420  # 7 минут
CRYPTOBOT_TOKEN = ('361366:AAX23ElQvhaHcWydcSeS764cmRWp43ikxNO')
CRYPTOBOT_CURRENCY = 'USDT'
ADMIN_ID = ['5864627885', '7783847586']
SELLER_SHARE = 0.6  # 60% продавцу
ADMIN_SHARE = 0.4    # 40% администратору

Base = declarative_base()

class Number(Base):
    __tablename__ = 'numbers'
    uid = Column(String(36), primary_key=True)
    country = Column(String)
    phone = Column(String)
    service = Column(String)
    seller_id = Column(String)
    price = Column(Float)
    status = Column(String, default='available')
    added_at = Column(DateTime, default=datetime.now)
    reserved_at = Column(DateTime)
    reserved_by = Column(String)
    sms_code = Column(String)
    crypto_invoice_id = Column(String)

class Transaction(Base):
    __tablename__ = 'transactions'
    uid = Column(String(36), primary_key=True)
    number_uid = Column(String(36))
    buyer_id = Column(String)
    seller_id = Column(String)
    amount = Column(Float)
    crypto_amount = Column(Float)
    crypto_currency = Column(String)
    status = Column(String, default='pending')
    created_at = Column(DateTime, default=datetime.now)
    completed_at = Column(DateTime)
    seller_invoice_id = Column(String)
    admin_invoice_id = Column(String)

class PriceLimit(Base):
    __tablename__ = 'price_limits'
    id = Column(Integer, primary_key=True)
    country = Column(String)
    service = Column(String)
    min_price = Column(Float)
    max_price = Column(Float)

# Инициализация БД
engine = create_engine('sqlite:///number_market_crypto.db', echo=True)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
session = Session()

# Создаем стандартные лимиты цен при первом запуске
if not session.query(PriceLimit).first():
    for country in COUNTRIES:
        for service in ['WhatsApp', 'Telegram']:
            session.add(PriceLimit(
                country=country,
                service=service,
                min_price=1.0,
                max_price=100.0
            ))
    session.commit()

def generate_uid():
    return str(uuid.uuid4())

def create_keyboard(items, row_width=2):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=row_width)
    buttons = [types.KeyboardButton(item) for item in items]
    markup.add(*buttons)
    return markup

def format_number_info(number):
    return (
        f"🆔 UID: <code>{number.uid}</code>\n"
        f"🌍 Страна: {number.country}\n"
        f"🔹 Сервис: {number.service}\n"
        f"📞 Номер: {number.phone}\n"
        f"💵 Цена: {number.price:.2f} USD\n"
    )

def validate_phone(country, phone):
    config = COUNTRIES.get(country)
    if not config:
        return False
    cleaned = re.sub(r'\D', '', phone)
    return len(cleaned) == config['length']

def create_crypto_invoice(amount_usd, description, chat_id=None):
    headers = {'Crypto-Pay-API-Token': CRYPTOBOT_TOKEN}
    params = {
        'amount': amount_usd,
        'asset': CRYPTOBOT_CURRENCY,
        'description': description,
        'paid_btn_url': f"https://t.me/{bot.get_me().username}",
        'allow_comments': False
    }
    
    if chat_id:
        params['paid_btn_url'] += f"?start=invoice_{chat_id}"
    
    try:
        response = requests.post(
            'https://pay.crypt.bot/api/createInvoice',
            headers=headers,
            json=params
        )
        data = response.json()
        if data.get('ok'):
            return {
                'invoice_id': data['result']['invoice_id'],
                'pay_url': data['result']['pay_url'],
                'amount': float(data['result']['amount']),
                'currency': data['result']['asset']
            }
        return None
    except Exception as e:
        print("Cryptobot API error:", str(e))
        return None

def check_crypto_payment(invoice_id):
    headers = {'Crypto-Pay-API-Token': CRYPTOBOT_TOKEN}
    params = {'invoice_ids': str(invoice_id)}
    try:
        response = requests.get(
            'https://pay.crypt.bot/api/getInvoices',
            headers=headers,
            params=params
        )
        data = response.json()
        if data.get('ok') and data['result']['items']:
            return data['result']['items'][0]['status'] == 'paid'
    except Exception as e:
        print("Cryptobot API error:", str(e))
    return False

def create_split_invoices(transaction):
    seller_amount = transaction.amount * SELLER_SHARE
    admin_amount = transaction.amount * ADMIN_SHARE
    
    seller_invoice = create_crypto_invoice(
        seller_amount,
        f"Оплата за номер {transaction.number_uid} (60%)",
        transaction.seller_id
    )
    
    admin_invoice = create_crypto_invoice(
        admin_amount,
        f"Комиссия за номер {transaction.number_uid} (40%)",
        ADMIN_ID
    )
    
    if seller_invoice and admin_invoice:
        transaction.seller_invoice_id = seller_invoice['invoice_id']
        transaction.admin_invoice_id = admin_invoice['invoice_id']
        session.commit()
        return True
    return False

def check_price_limits(country, service, price):
    limit = session.query(PriceLimit).filter_by(
        country=country,
        service=service.replace('📱 ', '').replace('✈️ ', '')
    ).first()
    
    if not limit:
        return True
    
    return limit.min_price <= price <= limit.max_price

# Админ-панель
@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if str(message.from_user.id) != ADMIN_ID:
        return
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(
        types.KeyboardButton('📊 Статистика'),
        types.KeyboardButton('⚙️ Лимиты цен'),
        types.KeyboardButton('🔙 В главное меню')
    )
    bot.send_message(
        message.chat.id,
        "👨‍💻 Админ-панель:",
        reply_markup=markup
    )

@bot.message_handler(func=lambda m: m.text == '🔙 В главное меню')
def back_to_main_menu(message):
    start(message)

@bot.message_handler(func=lambda m: m.text == '⚙️ Лимиты цен' and str(m.from_user.id) == ADMIN_ID)
def price_limits_menu(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for country in COUNTRIES:
        markup.add(types.KeyboardButton(f"🛠 {country}"))
    markup.add(types.KeyboardButton('🔙 Назад'))
    
    bot.send_message(
        message.chat.id,
        "Выберите страну для настройки лимитов:",
        reply_markup=markup
    )

@bot.message_handler(func=lambda m: m.text.startswith('🛠 ') and str(m.from_user.id) == ADMIN_ID)
def set_price_limits(message):
    country = message.text[2:]
    if country not in COUNTRIES:
        bot.send_message(message.chat.id, "❌ Неверная страна")
        return
    
    limits = session.query(PriceLimit).filter_by(country=country).all()
    if not limits:
        bot.send_message(message.chat.id, "❌ Лимиты не найдены")
        return
    
    text = f"📊 Лимиты цен для {country}:\n\n"
    for limit in limits:
        text += f"{limit.service}: {limit.min_price:.2f}-{limit.max_price:.2f} USD\n"
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for service in ['WhatsApp', 'Telegram']:
        markup.add(types.KeyboardButton(f"✏️ {country} {service}"))
    markup.add(types.KeyboardButton('🔙 Назад'))
    
    bot.send_message(
        message.chat.id,
        text,
        reply_markup=markup
    )

@bot.message_handler(func=lambda m: m.text == '🔙 Назад' and str(m.from_user.id) == ADMIN_ID)
def back_in_admin_menu(message):
    admin_panel(message)

@bot.message_handler(func=lambda m: m.text.startswith('✏️ ') and str(m.from_user.id) == ADMIN_ID)
def edit_price_limit(message):
    parts = message.text[2:].split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, "❌ Неверный формат")
        return
    
    country = ' '.join(parts[:-1])
    service = parts[-1]
    
    limit = session.query(PriceLimit).filter_by(
        country=country,
        service=service
    ).first()
    
    if not limit:
        bot.send_message(message.chat.id, "❌ Лимит не найден")
        return
    
    msg = bot.send_message(
        message.chat.id,
        f"Введите новый диапазон цен для {country} ({service}) в формате:\n"
        "<минимальная цена> <максимальная цена>\n"
        f"Текущие значения: {limit.min_price:.2f}-{limit.max_price:.2f} USD",
        reply_markup=types.ReplyKeyboardRemove()
    )
    bot.register_next_step_handler(msg, process_price_limit_update, limit)

def process_price_limit_update(message, limit):
    try:
        min_price, max_price = map(float, message.text.split())
        if min_price < 0 or max_price < min_price:
            raise ValueError
        
        limit.min_price = min_price
        limit.max_price = max_price
        session.commit()
        
        bot.send_message(
            message.chat.id,
            f"✅ Лимиты обновлены: {min_price:.2f}-{max_price:.2f} USD"
        )
    except Exception as e:
        bot.send_message(
            message.chat.id,
            f"❌ Ошибка: {str(e)}\nИспользуйте формат: 1.0 100.0"
        )

# Основные команды
@bot.message_handler(commands=['start'])
def start(message):
    if str(message.from_user.id) == ADMIN_ID:
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add(
            types.KeyboardButton('💰 Продать номер'),
            types.KeyboardButton('🛒 Купить номер'),
            types.KeyboardButton('📊 Мои номера'),
            types.KeyboardButton('👨‍💻 Админ-панель')
        )
    else:
        markup = create_keyboard(['💰 Продать номер', '🛒 Купить номер', '📊 Мои номера'])
    
    bot.send_message(
        message.chat.id,
        "🔢 Биржа номеров купля/продажа(USDT)\n\n"
        reply_markup=markup
    )

@bot.message_handler(func=lambda m: m.text == '👨‍💻 Админ-панель' and str(m.from_user.id) == ADMIN_ID)
def admin_panel_button(message):
    admin_panel(message)

@bot.message_handler(func=lambda m: m.text == '💰 Продать номер')
def sell_number_start(message):
    msg = bot.send_message(
        message.chat.id,
        "Выберите страну номера:",
        reply_markup=create_keyboard(COUNTRIES.keys())
    )
    bot.register_next_step_handler(msg, process_sell_country)

def process_sell_country(message):
    if message.text not in COUNTRIES:
        msg = bot.send_message(
            message.chat.id,
            "Пожалуйста, выберите страну из списка:",
            reply_markup=create_keyboard(COUNTRIES.keys())
        )
        bot.register_next_step_handler(msg, process_sell_country)
        return
    
    user_data = {'country': message.text}
    msg = bot.send_message(
        message.chat.id,
        "Выберите сервис:",
        reply_markup=create_keyboard(SERVICES)
    )
    bot.register_next_step_handler(msg, process_sell_service, user_data)

def process_sell_service(message, user_data):
    clean_service = message.text.replace('📱 ', '').replace('✈️ ', '')
    if clean_service not in ['WhatsApp', 'Telegram']:
        msg = bot.send_message(
            message.chat.id,
            "Пожалуйста, выберите сервис из списка:",
            reply_markup=create_keyboard(SERVICES)
        )
        bot.register_next_step_handler(msg, process_sell_service, user_data)
        return
    
    user_data['service'] = clean_service
    example = COUNTRIES[user_data['country']]['example']
    msg = bot.send_message(
        message.chat.id,
        f"Введите номер телефона (только цифры, с кодом страны пример: +7{example}):",
        reply_markup=types.ReplyKeyboardRemove()
    )
    bot.register_next_step_handler(msg, process_sell_phone, user_data)

def process_sell_phone(message, user_data):
    if not validate_phone(user_data['country'], message.text):
        country_config = COUNTRIES.get(user_data['country'])
        msg = bot.send_message(
            message.chat.id,
            f"❌ Неверный формат номера для {user_data['country']}.\n"
            f"Требуется {country_config['length']} цифр с кодом страны (пример: +7{country_config['example']}).\n"
            "Введите еще раз:"
        )
        bot.register_next_step_handler(msg, process_sell_phone, user_data)
        return
    
    user_data['phone'] = message.text
    msg = bot.send_message(
        message.chat.id,
        "Введите цену в долларах USA (например: 5.50):"
    )
    bot.register_next_step_handler(msg, process_sell_price, user_data)

def process_sell_price(message, user_data):
    try:
        price = float(message.text)
        
        # Проверка лимитов цены
        if not check_price_limits(user_data['country'], user_data['service'], price):
            limit = session.query(PriceLimit).filter_by(
                country=user_data['country'],
                service=user_data['service']
            ).first()
            
            bot.send_message(
                message.chat.id,
                f"❌ Цена должна быть в диапазоне {limit.min_price:.2f}-{limit.max_price:.2f} USD"
            )
            return
            
        if price <= 0:
            raise ValueError
            
        uid = generate_uid()
        
        new_number = Number(
            uid=uid,
            country=user_data['country'],
            phone=user_data['phone'],
            service=user_data['service'],
            seller_id=str(message.from_user.id),
            price=price
        )
        
        session.add(new_number)
        session.commit()
        
        bot.send_message(
            message.chat.id,
            f"✅ Номер успешно выставлен на продажу!\n\n"
            f"{format_number_info(new_number)}\n"
            "Теперь покупатели смогут найти ваш номер по UID",
            parse_mode='HTML'
        )
    except ValueError:
        bot.send_message(message.chat.id, "❌ Неверный формат цены. Введите положительное число.")

@bot.message_handler(func=lambda m: m.text == '🛒 Купить номер')
def buy_number_start(message):
    msg = bot.send_message(
        message.chat.id,
        "Выберите страну:",
        reply_markup=create_keyboard(COUNTRIES.keys())
    )
    bot.register_next_step_handler(msg, process_buy_country)

def process_buy_country(message):
    if message.text not in COUNTRIES:
        msg = bot.send_message(
            message.chat.id,
            "Пожалуйста, выберите страну из списка:",
            reply_markup=create_keyboard(COUNTRIES.keys())
        )
        bot.register_next_step_handler(msg, process_buy_country)
        return
    
    user_data = {'country': message.text}
    msg = bot.send_message(
        message.chat.id,
        "Выберите сервис:",
        reply_markup=create_keyboard(SERVICES)
    )
    bot.register_next_step_handler(msg, process_buy_service, user_data)

def process_buy_service(message, user_data):
    clean_service = message.text.replace('📱 ', '').replace('✈️ ', '')
    if clean_service not in ['WhatsApp', 'Telegram']:
        msg = bot.send_message(
            message.chat.id,
            "Пожалуйста, выберите сервис из списка:",
            reply_markup=create_keyboard(SERVICES)
        )
        bot.register_next_step_handler(msg, process_buy_service, user_data)
        return
    
    user_data['service'] = clean_service
    show_available_numbers(message, user_data)

def show_available_numbers(message, user_data):
    numbers = session.query(Number).filter_by(
        country=user_data['country'],
        service=user_data['service'],
        status='available'
    ).order_by(Number.added_at.desc()).limit(5).all()
    
    if not numbers:
        bot.send_message(
            message.chat.id,
            f"😕 Нет доступных номеров для {user_data['service']} ({user_data['country']}).",
            reply_markup=types.ReplyKeyboardRemove()
        )
        return
    
    for num in numbers:
        bot.send_message(
            message.chat.id,
            f"{format_number_info(num)}\n"
            "Для покупки отправьте:\n"
            f"<code>/buy_{num.uid}</code>",
            parse_mode='HTML',
            reply_markup=types.ReplyKeyboardRemove()
        )

@bot.message_handler(func=lambda m: m.text.startswith('/buy_'))
def reserve_number(message):
    try:
        uid = message.text.split('_')[1]
        
        number = session.query(Number).filter_by(uid=uid).first()
        
        if not number:
            bot.send_message(message.chat.id, "❌ Номер с таким UID не найден.")
            return
            
        if number.status != 'available':
            bot.send_message(message.chat.id, "❌ Этот номер уже куплен или зарезервирован.")
            return
            
        # Создаем крипто-инвойс
        invoice = create_crypto_invoice(number.price, f"Покупка номера {uid}")
        if not invoice:
            bot.send_message(message.chat.id, "❌ Ошибка создания платежа. Попробуйте позже.")
            return
        
        number.status = 'reserved'
        number.reserved_at = datetime.now()
        number.reserved_by = str(message.from_user.id)
        number.crypto_invoice_id = invoice['invoice_id']
        session.commit()
        
        # Создаем транзакцию
        transaction = Transaction(
            uid=generate_uid(),
            number_uid=number.uid,
            buyer_id=str(message.from_user.id),
            seller_id=number.seller_id,
            amount=number.price,
            crypto_amount=invoice['amount'],
            crypto_currency=invoice['currency'],
            status='invoice_created'
        )
        session.add(transaction)
        session.commit()
        
        markup = types.InlineKeyboardMarkup()
        btn_pay = types.InlineKeyboardButton(
            f"💳 Оплатить {invoice['amount']:.2f} {invoice['currency']}", 
            url=invoice['pay_url']
        )
        markup.add(btn_pay)
        
        bot.send_message(
            message.chat.id,
            f"⏳ Номер зарезервирован на {RESERVE_TIME//60} минут!\n\n"
            f"{format_number_info(number)}\n"
            f"💵 Сумма к оплате: {invoice['amount']:.2f} {invoice['currency']}\n"
            f"💰 Сумма в USD: {number.price:.2f}\n\n"
            "Нажмите кнопку ниже для оплаты:",
            reply_markup=markup,
            parse_mode='HTML'
        )
        
        # Кнопка подтверждения оплаты
        markup = types.InlineKeyboardMarkup()
        btn_confirm = types.InlineKeyboardButton(
            "✅ Я оплатил", 
            callback_data=f"confirm_{transaction.uid}"
        )
        markup.add(btn_confirm)
        
        bot.send_message(
            message.chat.id,
            "После оплаты нажмите кнопку ниже:",
            reply_markup=markup
        )
        
        threading.Timer(RESERVE_TIME, check_transaction, args=[uid]).start()
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {str(e)}\nФормат: /buy_UID")

@bot.callback_query_handler(func=lambda call: call.data.startswith('confirm_'))
def confirm_payment(call):
    try:
        transaction_uid = call.data.split('_')[1]
        transaction = session.query(Transaction).filter_by(uid=transaction_uid).first()
        
        if not transaction:
            bot.answer_callback_query(call.id, "❌ Транзакция не найдена")
            return
            
        number = session.query(Number).filter_by(uid=transaction.number_uid).first()
        if not check_crypto_payment(number.crypto_invoice_id):
            bot.answer_callback_query(call.id, "❌ Оплата не найдена")
            return
        
        # Создаем раздельные чеки
        if not create_split_invoices(transaction):
            bot.answer_callback_query(call.id, "❌ Ошибка создания чеков")
            return
        
        transaction.status = 'paid'
        number.status = 'code_waiting'
        session.commit()
        
        # Сообщение покупателю
        bot.send_message(
            transaction.buyer_id,
            f"✅ Оплата подтверждена!\n\n"
            f"{format_number_info(number)}\n"
            "Ожидайте код SMS от продавца.",
            parse_mode='HTML'
        )
        
        # Чек для продавца (60%)
        seller_amount = transaction.amount * SELLER_SHARE
        markup = types.InlineKeyboardMarkup()
        btn_seller = types.InlineKeyboardButton(
            f"💰 Вывести {seller_amount:.2f} {transaction.crypto_currency}",
            callback_data=f"withdraw_seller_{transaction.uid}"
        )
        markup.add(btn_seller)
        
        bot.send_message(
            transaction.seller_id,
            f"🔢 Оплачен номер:\n{format_number_info(number)}\n"
            f"💸 Полная сумма: {transaction.amount:.2f} USD\n"
            f"💰 Ваша доля: {seller_amount:.2f} {transaction.crypto_currency}\n\n"
            "Отправьте код SMS покупателю командой:\n"
            f"<code>/send_code_{number.uid} КОД</code>",
            reply_markup=markup,
            parse_mode='HTML'
        )
        
        # Чек для админа (40%)
        admin_amount = transaction.amount * ADMIN_SHARE
        markup = types.InlineKeyboardMarkup()
        btn_admin = types.InlineKeyboardButton(
            f"💼 Вывести {admin_amount:.2f} {transaction.crypto_currency}",
            callback_data=f"withdraw_admin_{transaction.uid}"
        )
        markup.add(btn_admin)
        
        bot.send_message(
            ADMIN_ID,
            f"💰 Комиссия за номер:\n{format_number_info(number)}\n"
            f"💼 Ваша доля: {admin_amount:.2f} {transaction.crypto_currency}",
            reply_markup=markup,
            parse_mode='HTML'
        )
        
        bot.answer_callback_query(call.id, "✅ Оплата подтверждена")
        
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ Ошибка: {str(e)}")

@bot.callback_query_handler(func=lambda call: call.data.startswith('withdraw_seller_'))
def withdraw_seller(call):
    try:
        transaction_uid = call.data.split('_')[2]
        transaction = session.query(Transaction).filter_by(uid=transaction_uid).first()
        
        if not transaction:
            bot.answer_callback_query(call.id, "❌ Транзакция не найдена")
            return
            
        if not transaction.seller_invoice_id:
            bot.answer_callback_query(call.id, "❌ Чек для продавца не создан")
            return
            
        # Проверяем оплату чека продавца
        if check_crypto_payment(transaction.seller_invoice_id):
            bot.answer_callback_query(call.id, "✅ Средства уже получены")
            return
            
        # Создаем ссылку для получения средств
        seller_amount = transaction.amount * SELLER_SHARE
        withdraw_url = f"https://t.me/CryptoBot?start=withdraw_{transaction.seller_invoice_id}"
        
        bot.send_message(
            call.message.chat.id,
            f"💸 Для получения {seller_amount:.2f} {transaction.crypto_currency}:\n"
            f"1. Откройте @CryptoBot\n"
            f"2. Нажмите 'Start'\n"
            f"3. Или перейдите по прямой ссылке: {withdraw_url}",
            disable_web_page_preview=True
        )
        
        bot.answer_callback_query(call.id, "✅ Инструкция для получения средств отправлена")
        
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ Ошибка: {str(e)}")

@bot.callback_query_handler(func=lambda call: call.data.startswith('withdraw_admin_'))
def withdraw_admin(call):
    try:
        transaction_uid = call.data.split('_')[2]
        transaction = session.query(Transaction).filter_by(uid=transaction_uid).first()
        
        if not transaction:
            bot.answer_callback_query(call.id, "❌ Транзакция не найдена")
            return
            
        if not transaction.admin_invoice_id:
            bot.answer_callback_query(call.id, "❌ Чек для администратора не создан")
            return
            
        # Проверяем оплату чека админа
        if check_crypto_payment(transaction.admin_invoice_id):
            bot.answer_callback_query(call.id, "✅ Средства уже получены")
            return
            
        # Создаем ссылку для получения средств
        admin_amount = transaction.amount * ADMIN_SHARE
        withdraw_url = f"https://t.me/CryptoBot?start=withdraw_{transaction.admin_invoice_id}"
        
        bot.send_message(
            call.message.chat.id,
            f"💸 Для получения {admin_amount:.2f} {transaction.crypto_currency}:\n"
            f"1. Откройте @CryptoBot\n"
            f"2. Нажмите 'Start'\n"
            f"3. Или перейдите по прямой ссылке: {withdraw_url}",
            disable_web_page_preview=True
        )
        
        bot.answer_callback_query(call.id, "✅ Инструкция для получения средств отправлена")
        
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ Ошибка: {str(e)}")

def get_invoice_info(invoice_id):
    """Получаем информацию о чеке из Cryptobot"""
    headers = {'Crypto-Pay-API-Token': CRYPTOBOT_TOKEN}
    params = {'invoice_ids': str(invoice_id)}
    
    try:
        response = requests.get(
            'https://pay.crypt.bot/api/getInvoices',
            headers=headers,
            params=params
        )
        data = response.json()
        
        if data.get('ok') and data['result']['items']:
            return {
                'pay_url': data['result']['items'][0]['pay_url'],
                'amount': data['result']['items'][0]['amount'],
                'status': data['result']['items'][0]['status']
            }
    except Exception as e:
        print(f"Ошибка получения информации о чеке: {str(e)}")
    
    return None
@bot.message_handler(func=lambda m: m.text.startswith('/send_code_'))
def send_sms_code(message):
    try:
        parts = message.text.split('_')
        uid = parts[2].split()[0]
        sms_code = ' '.join(parts[2].split()[1:])
        
        number = session.query(Number).filter_by(uid=uid).first()
        transaction = session.query(Transaction).filter_by(number_uid=uid, status='paid').first()
        
        if not number or not transaction:
            bot.send_message(message.chat.id, "❌ Номер или транзакция не найдена.")
            return
        
        if str(message.from_user.id) != number.seller_id:
            bot.send_message(message.chat.id, "❌ Вы не продавец этого номера.")
            return
        
        number.sms_code = sms_code
        transaction.status = 'code_sent'
        session.commit()
        
        bot.send_message(
            transaction.buyer_id,
            f"🔢 Продавец отправил код для номера:\n\n"
            f"{format_number_info(number)}\n"
            f"🔢 Код: {sms_code}\n\n"
            "Подтвердите получение кода командой:\n"
            f"<code>/confirm_code_{number.uid}</code>",
            parse_mode='HTML'
        )
        
        bot.send_message(
            message.chat.id,
            "✅ Код отправлен покупателю. Ожидайте подтверждения."
        )
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {str(e)}\nФормат: /send_code_UID КОД")

@bot.message_handler(func=lambda m: m.text.startswith('/confirm_code_'))
def confirm_code_received(message):
    try:
        uid = message.text.split('_')[2]
        
        number = session.query(Number).filter_by(uid=uid).first()
        transaction = session.query(Transaction).filter_by(number_uid=uid, status='code_sent').first()
        
        if not number or not transaction:
            bot.send_message(message.chat.id, "❌ Номер или транзакция не найдена.")
            return
        
        if str(message.from_user.id) != transaction.buyer_id:
            bot.send_message(message.chat.id, "❌ Это не ваш номер.")
            return
        
        transaction.status = 'completed'
        transaction.completed_at = datetime.now()
        number.status = 'completed'
        session.commit()
        
        bot.send_message(
            message.chat.id,
            f"🎉 Сделка завершена!\n\n"
            f"{format_number_info(number)}\n"
            f"🔢 Код: {number.sms_code}\n\n"
            "Спасибо за покупку!",
            parse_mode='HTML'
        )
        
        bot.send_message(
            transaction.seller_id,
            f"✅ Покупатель подтвердил получение кода:\n\n"
            f"{format_number_info(number)}\n"
            f"💰 Сумма: {transaction.amount:.2f} USD\n"
            f"🔢 Код: {number.sms_code}\n\n"
            "Сделка успешно завершена.",
            parse_mode='HTML'
        )
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {str(e)}\nФормат: /confirm_code_UID")

def check_transaction(uid):
    number = session.query(Number).filter_by(uid=uid).first()
    if number and number.status == 'reserved':
        number.status = 'available'
        number.reserved_at = None
        number.reserved_by = None
        number.crypto_invoice_id = None
        session.commit()
        
        bot.send_message(
            number.reserved_by,
            f"⌛️ Время резерва номера истекло:\n\n"
            f"{format_number_info(number)}\n"
            "Номер снова доступен для покупки.",
            parse_mode='HTML'
        )

@bot.message_handler(func=lambda m: m.text == '📊 Мои номера')
def show_user_numbers(message):
    # Номера, которые пользователь продает
    selling_numbers = session.query(Number).filter_by(
        seller_id=str(message.from_user.id)
    ).order_by(Number.added_at.desc()).all()
    
    # Номера, которые пользователь купил
    bought_numbers = session.query(Number).filter_by(
        reserved_by=str(message.from_user.id),
        status='completed'
    ).order_by(Number.reserved_at.desc()).all()
    
    if not selling_numbers and not bought_numbers:
        bot.send_message(message.chat.id, "У вас нет номеров.")
        return
    
    if selling_numbers:
        bot.send_message(message.chat.id, "🛒 Ваши номера на продаже:")
        for num in selling_numbers:
            status = "🟢 Доступен" if num.status == 'available' else \
                   "🟡 Зарезервирован" if num.status == 'reserved' else \
                   "🔴 Продан"
            
            bot.send_message(
                message.chat.id,
                f"{format_number_info(num)}\n"
                f"📊 Статус: {status}\n"
                f"🕒 Добавлен: {num.added_at.strftime('%d.%m.%Y %H:%M')}",
                parse_mode='HTML'
            )
    
    if bought_numbers:
        bot.send_message(message.chat.id, "🛍 Ваши купленные номера:")
        for num in bought_numbers:
            bot.send_message(
                message.chat.id,
                f"{format_number_info(num)}\n"
                f"🔢 Код: {num.sms_code}\n"
                f"🕒 Куплен: {num.reserved_at.strftime('%d.%m.%Y %H:%M')}",
                parse_mode='HTML'
            )

if __name__ == '__main__':
    print("Бот с полной функциональностью запущен!")
    bot.infinity_polling()
