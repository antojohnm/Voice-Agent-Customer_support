import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
import os
import redis
import time
from datetime import datetime

load_dotenv()

# ── Redis connection ──
redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    password=os.getenv("REDIS_PASSWORD") or None,
    decode_responses=True
)

# ── Redis TTL — all call keys auto-expire after 15 minutes ──
REDIS_TTL = 900
CATEGORIES_CACHE_TTL = 300  # 5 minutes instead of 30

def invalidate_categories_cache():
    """
    Call this whenever products are added, updated, or removed.
    Forces the next request to fetch fresh data from the database.
    """
    try:
        redis_client.delete("global:product_categories")
        print("Product categories cache invalidated")
    except Exception as e:
        print(f"Could not invalidate categories cache: {e}")

def add_product(product_name, category, price, stock_available=True):
    """Add a new product and immediately invalidate categories cache"""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO product_catalog (product_name, category, price, stock_available)
        VALUES (%s, %s, %s, %s)
    """, (product_name, category, price, stock_available))

    conn.commit()
    cursor.close()
    conn.close()

    # Immediately invalidate cache so next caller gets fresh data
    invalidate_categories_cache()
    print(f"Product '{product_name}' added and cache invalidated")


def update_product_availability(product_id, is_available):
    """Update stock availability and invalidate cache"""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE product_catalog
        SET stock_available = %s
        WHERE product_id = %s
    """, (is_available, product_id))

    conn.commit()
    cursor.close()
    conn.close()

    # Invalidate cache immediately
    invalidate_categories_cache()

# ════════════════════════════════════════════════════
# Redis helper functions
# ════════════════════════════════════════════════════

def set_call_state(call_sid: str, is_speaking: bool, host: str = ""):
    """Initialise call state in Redis when stream opens."""
    redis_client.hset(f"call:{call_sid}", mapping={
        "is_speaking":      int(is_speaking),
        "host":             host,
        "resumed_at":       "0",
        "last_activity_at": str(time.time()),
        "call_started_at":  str(time.time()),
    })
    redis_client.expire(f"call:{call_sid}", REDIS_TTL)


def get_call_state(call_sid: str) -> dict:
    """Read full call state from Redis. Returns empty dict if not found."""
    state = redis_client.hgetall(f"call:{call_sid}")
    if not state:
        return {}
    return {
        "is_speaking":      bool(int(state.get("is_speaking", 0))),
        "host":             state.get("host", ""),
        "resumed_at":       float(state.get("resumed_at", 0)),
        "last_activity_at": float(state.get("last_activity_at", 0)),
        "call_started_at":  float(state.get("call_started_at", 0)),
    }


def update_call_state(call_sid: str, **kwargs):
    """Update one or more fields in call state."""
    update = {}
    if "is_speaking" in kwargs:
        update["is_speaking"] = int(kwargs["is_speaking"])
    if "host" in kwargs:
        update["host"] = kwargs["host"]
    if "resumed_at" in kwargs:
        update["resumed_at"] = str(kwargs["resumed_at"])
    if "last_activity_at" in kwargs:
        update["last_activity_at"] = str(kwargs["last_activity_at"])
    if update:
        redis_client.hset(f"call:{call_sid}", mapping=update)


def delete_call_state(call_sid: str):
    """Remove all Redis keys for a call when it ends."""
    redis_client.delete(f"call:{call_sid}")
    redis_client.delete(f"order_context:{call_sid}")
    redis_client.delete(f"responses:{call_sid}")
    print(f"[{call_sid}] Redis cleared")


# ════════════════════════════════════════════════════
# Keyword intent definitions
# ════════════════════════════════════════════════════

def detect_intent(transcript: str):
    """
    Match only on full phrases to avoid false positives.
    Returns intent string or None if ambiguous/complex.
    """
    transcript_lower = transcript.lower().strip()

    simple_status_phrases = [
        "what is the status", "status of my order", "where is my order",
        "has it shipped", "is it shipped", "current status",
        "order status", "what stage is", "what happened to my order",
        "order update", "track my order"
    ]
    simple_delivery_phrases = [
        "delivery address", "where will it be delivered",
        "what address", "which address", "shipping address",
        "where is it going", "where will my order be delivered",
        "what is the delivery address"
    ]
    simple_payment_phrases = [
        "payment status", "did i pay", "was i charged",
        "payment details", "how did i pay", "payment method",
        "was the payment successful", "payment information"
    ]
    simple_items_phrases = [
        "what did i order", "what items", "list of items",
        "what products", "items in my order", "what have i ordered",
        "what is in my order", "show me my items"
    ]
    simple_date_phrases = [
        "when did i order", "order date", "when was my order placed",
        "date of my order", "when did i place", "what date was my order"
    ]

    if any(phrase in transcript_lower for phrase in simple_status_phrases):
        return "status"
    if any(phrase in transcript_lower for phrase in simple_delivery_phrases):
        return "delivery"
    if any(phrase in transcript_lower for phrase in simple_payment_phrases):
        return "payment"
    if any(phrase in transcript_lower for phrase in simple_items_phrases):
        return "items"
    if any(phrase in transcript_lower for phrase in simple_date_phrases):
        return "date"

    return None


# ════════════════════════════════════════════════════
# Order context caching
# ════════════════════════════════════════════════════

def get_cached_response(call_sid: str, transcript: str):
    """
    Check Redis for a cached response matching the transcript's intent.
    Returns response string if found, None if cache miss.
    """
    intent = detect_intent(transcript)
    if not intent:
        return None

    cached = redis_client.hget(f"responses:{call_sid}", intent)
    if cached:
        print(f"[{call_sid}] Redis cache hit — intent: '{intent}'")
        return cached

    return None


def store_llm_response(call_sid: str, transcript: str, response: str):
    """
    Lazily store LLM response in Redis after generation.
    Only stores if intent is clear and unambiguous.
    Builds cache turn by turn as questions are asked.
    """
    intent = detect_intent(transcript)
    if not intent:
        return

    redis_client.hset(f"responses:{call_sid}", mapping={
        intent:                        response,
        f"{intent}_cached_at":         str(time.time()),
        f"{intent}_original_question": transcript
    })
    redis_client.expire(f"responses:{call_sid}", REDIS_TTL)
    print(f"[{call_sid}] Lazily cached response for intent: '{intent}'")

def verify_customer_phone(
    order_id,
    caller_number
):
    """
    Verify that the incoming caller owns
    the requested order.
    """

    conn = get_connection()

    try:
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT c.phone
            FROM orders o
            JOIN customers c
                ON o.customer_id = c.customer_id
            WHERE o.order_id = %s
            """,
            (order_id,)
        )

        row = cursor.fetchone()

        cursor.close()

        if not row:
            return False

        registered_phone = str(row[0]).strip()
        caller_number = str(caller_number).strip()

        return registered_phone == caller_number

    finally:
        conn.close()

def get_order_context_cached(order_id: int, call_sid: str):
    """
    Get order context — checks Redis first, falls back to PostgreSQL.
    Caches result in Redis for the duration of the call.
    """
    cache_key = f"order_context:{call_sid}"

    cached = redis_client.get(cache_key)
    if cached:
        print(f"[{call_sid}] Order context cache hit")
        return cached

    print(f"[{call_sid}] Order context cache miss — fetching from PostgreSQL")
    context = get_order_context(order_id)

    if context:
        redis_client.setex(cache_key, REDIS_TTL, context)

    return context


# ════════════════════════════════════════════════════
# PostgreSQL connection
# ════════════════════════════════════════════════════

def get_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "call_centre"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD")
    )


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS calls (
            id SERIAL PRIMARY KEY,
            call_sid TEXT UNIQUE NOT NULL,
            caller_number TEXT,
            started_at TIMESTAMP,
            ended_at TIMESTAMP,
            status TEXT DEFAULT 'active',
            recording_url TEXT,
            recording_sid TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            call_sid TEXT UNIQUE NOT NULL REFERENCES calls(call_sid),
            conversation TEXT NOT NULL,
            last_updated TIMESTAMP NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS call_verifications (
            call_sid TEXT PRIMARY KEY REFERENCES calls(call_sid),
            customer_id INTEGER,
            verified_at TIMESTAMP,
            voice_code TEXT
        )
    """)

    conn.commit()
    cursor.close()
    conn.close()
    print("PostgreSQL database initialized")


def start_call(call_sid, caller_number):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO calls (call_sid, caller_number, started_at, status)
        VALUES (%s, %s, %s, 'active')
        ON CONFLICT (call_sid) DO NOTHING
    """, (call_sid, caller_number, datetime.now()))
    conn.commit()
    cursor.close()
    conn.close()
    print(f"Call started: {call_sid} from {caller_number}")


def end_call(call_sid):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE calls SET ended_at = %s, status = 'ended'
        WHERE call_sid = %s
    """, (datetime.now(), call_sid))
    conn.commit()
    cursor.close()
    conn.close()
    print(f"Call ended: {call_sid}")


def save_recording(call_sid, recording_url, recording_sid):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE calls SET recording_url = %s, recording_sid = %s
        WHERE call_sid = %s
    """, (recording_url, recording_sid, call_sid))
    conn.commit()
    cursor.close()
    conn.close()
    print(f"Recording saved for {call_sid}: {recording_url}")


def save_message(call_sid, role, content):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT conversation FROM messages WHERE call_sid = %s
    """, (call_sid,))
    row = cursor.fetchone()

    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    new_line = f"[{timestamp}] {role.upper()}: {content}"

    if row:
        updated = row[0] + "\n" + new_line
        cursor.execute("""
            UPDATE messages SET conversation = %s, last_updated = %s
            WHERE call_sid = %s
        """, (updated, datetime.now(), call_sid))
    else:
        cursor.execute("""
            INSERT INTO messages (call_sid, conversation, last_updated)
            VALUES (%s, %s, %s)
        """, (call_sid, new_line, datetime.now()))

    conn.commit()
    cursor.close()
    conn.close()


def get_product_offers(product_name: str = None, category: str = None):
    """Get active promotions — by product name or category."""
    conn = get_connection()
    cursor = conn.cursor()

    if product_name:
        cursor.execute("""
            SELECT p.product_name, p.category, p.price,
                   o.offer_name, o.discount_value, o.end_date
            FROM promotions_offers o
            JOIN product_catalog p ON o.product_id = p.product_id
            WHERE o.end_date >= CURRENT_DATE
            AND LOWER(p.product_name) LIKE LOWER(%s)
            ORDER BY o.discount_value DESC
        """, (f"%{product_name}%",))
    elif category:
        cursor.execute("""
            SELECT p.product_name, p.category, p.price,
                   o.offer_name, o.discount_value, o.end_date
            FROM promotions_offers o
            JOIN product_catalog p ON o.product_id = p.product_id
            WHERE o.end_date >= CURRENT_DATE
            AND LOWER(p.category) LIKE LOWER(%s)
            ORDER BY o.discount_value DESC
        """, (f"%{category}%",))
    else:
        cursor.execute("""
            SELECT p.product_name, p.category, p.price,
                   o.offer_name, o.discount_value, o.end_date
            FROM promotions_offers o
            JOIN product_catalog p ON o.product_id = p.product_id
            WHERE o.end_date >= CURRENT_DATE
            ORDER BY o.discount_value DESC
        """)

    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    if not rows:
        return None

    lines = ["ACTIVE OFFERS:"]
    for row in rows:
        prod_name, cat, price, offer, discount, end_date = row
        lines.append(
            f"  - {prod_name} ({cat}): {offer} — "
            f"₹{discount} off | Valid until {end_date.strftime('%B %d, %Y')}"
        )
    return "\n".join(lines)


def get_return_policy(product_name: str = None):
    """Get return/refund policy for a product."""
    conn = get_connection()
    cursor = conn.cursor()

    if product_name:
        cursor.execute("""
            SELECT p.product_name, r.return_window_days,
                   r.exchange_allowed, r.policy_description
            FROM return_refund_policies r
            JOIN product_catalog p ON r.product_id = p.product_id
            WHERE LOWER(p.product_name) LIKE LOWER(%s)
        """, (f"%{product_name}%",))
    else:
        cursor.execute("""
            SELECT p.product_name, r.return_window_days,
                   r.exchange_allowed, r.policy_description
            FROM return_refund_policies r
            JOIN product_catalog p ON r.product_id = p.product_id
            LIMIT 1
        """)

    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row:
        return None

    prod_name, return_days, exchange, description = row
    exchange_str = "Yes" if exchange else "No"
    return (
        f"RETURN POLICY for {prod_name}:\n"
        f"  Return window: {return_days} days\n"
        f"  Exchange allowed: {exchange_str}\n"
        f"  Policy: {description}"
    )


def get_warranty(product_name: str = None):
    """Get warranty information for a product."""
    conn = get_connection()
    cursor = conn.cursor()

    if product_name:
        cursor.execute("""
            SELECT p.product_name, w.warranty_period, w.coverage_details
            FROM warranty_information w
            JOIN product_catalog p ON w.product_id = p.product_id
            WHERE LOWER(p.product_name) LIKE LOWER(%s)
        """, (f"%{product_name}%",))
    else:
        cursor.execute("""
            SELECT p.product_name, w.warranty_period, w.coverage_details
            FROM warranty_information w
            JOIN product_catalog p ON w.product_id = p.product_id
            LIMIT 1
        """)

    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row:
        return None

    prod_name, period, coverage = row
    return (
        f"WARRANTY for {prod_name}:\n"
        f"  Period: {period}\n"
        f"  Coverage: {coverage}"
    )


def get_store_info(city: str = None):
    """Get store locations — optionally filtered by city."""
    conn = get_connection()
    cursor = conn.cursor()

    if city:
        cursor.execute("""
            SELECT store_name, city, opening_time, closing_time
            FROM store_locations
            WHERE LOWER(city) LIKE LOWER(%s)
            ORDER BY city
        """, (f"%{city}%",))
    else:
        cursor.execute("""
            SELECT store_name, city, opening_time, closing_time
            FROM store_locations
            ORDER BY city
        """)

    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    if not rows:
        return None

    lines = ["STORE LOCATIONS:"]
    for row in rows:
        store_name, city_name, opening, closing = row
        lines.append(
            f"  - {store_name}, {city_name}: "
            f"Open {opening.strftime('%I:%M %p')} — {closing.strftime('%I:%M %p')}"
        )
    return "\n".join(lines)

def get_product_categories():
    """
    Fetch all available product categories and their products.
    Cached globally in Redis for 30 minutes.
    """
    cache_key = "global:product_categories"
    CATEGORIES_CACHE_TTL = 1800

    # Check Redis first
    try:
        cached = redis_client.get(cache_key)
        if cached:
            print("Product categories served from Redis cache")
            return cached
    except Exception as e:
        print(f"Redis unavailable for categories: {e}")

    # Cache miss — query database
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT category,
               string_agg(product_name, ', ' ORDER BY product_name) AS products
        FROM product_catalog
        WHERE stock_available = true
        GROUP BY category
        ORDER BY category
    """)

    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    if not rows:
        return ""

    lines = ["CATEGORIES AND PRODUCTS WE CURRENTLY CARRY:"]
    for category, products in rows:
        lines.append(f"- {category}: {products}")

    result = "\n".join(lines)

    # Store in Redis
    try:
        redis_client.setex(cache_key, CATEGORIES_CACHE_TTL, result)
        print("Product categories cached in Redis for 30 minutes")
    except Exception as e:
        print(f"Could not cache categories: {e}")

    return result


def invalidate_categories_cache():
    """Call this whenever products are added or updated."""
    try:
        redis_client.delete("global:product_categories")
        print("Product categories cache invalidated")
    except Exception as e:
        print(f"Could not invalidate cache: {e}")

def get_conversation_history(call_sid):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT conversation FROM messages WHERE call_sid = %s
    """, (call_sid,))

    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row or not row[0]:
        return []

    history = []
    for line in row[0].split("\n"):
        try:
            parts = line.split("] ", 1)
            if len(parts) < 2:
                continue
            rest = parts[1]
            role_part, content = rest.split(": ", 1)
            role = role_part.lower()
            history.append({"role": role, "content": content})
        except:
            continue

    return history


def get_all_calls():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT c.call_sid, c.caller_number, c.started_at, c.ended_at,
               c.status, c.recording_url, COUNT(m.id) as message_count
        FROM calls c
        LEFT JOIN messages m ON c.call_sid = m.call_sid
        GROUP BY c.call_sid
        ORDER BY c.started_at DESC
    """)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


def get_call_transcript(call_sid):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT conversation, last_updated FROM messages WHERE call_sid = %s
    """, (call_sid,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row[0] if row else "No transcript available."


def get_order_context(order_id):
    """
    Pull everything related to an order from all business tables.
    Returns a formatted string ready to be injected into the LLM prompt.
    Returns None if order not found.
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT o.order_id, o.order_status, o.total_amount, o.created_at,
               c.name, c.phone, c.email, c.address
        FROM orders o
        JOIN customers c ON o.customer_id = c.customer_id
        WHERE o.order_id = %s
    """, (order_id,))
    order_row = cursor.fetchone()

    if not order_row:
        cursor.close()
        conn.close()
        return None

    order_id_db, order_status, total_amount, created_at, \
        cust_name, cust_phone, cust_email, cust_address = order_row

    cursor.execute("""
        SELECT i.item_name, oi.quantity, oi.price_at_purchase,
               i.is_available, i.quantity as stock_quantity
        FROM order_items oi
        JOIN inventory i ON oi.item_id = i.item_id
        WHERE oi.order_id = %s
    """, (order_id,))
    items = cursor.fetchall()

    cursor.execute("""
        SELECT p.product_name, p.category,
               w.warranty_period,
               r.return_window_days, r.exchange_allowed
        FROM order_items oi
        JOIN inventory i ON oi.item_id = i.item_id
        LEFT JOIN product_catalog p ON i.product_id = p.product_id
        LEFT JOIN warranty_information w ON p.product_id = w.product_id
        LEFT JOIN return_refund_policies r ON p.product_id = r.product_id
        WHERE oi.order_id = %s
    """, (order_id,))
    product_details = cursor.fetchall()

    cursor.execute("""
        SELECT payment_method, payment_status, amount, paid_at
        FROM payments
        WHERE order_id = %s
        ORDER BY paid_at DESC
        LIMIT 1
    """, (order_id,))
    payment = cursor.fetchone()

    cursor.execute("""
        SELECT delivery_status, delivery_address, delivered_at,
               expected_delivery_date
        FROM deliveries
        WHERE order_id = %s
        ORDER BY delivery_id DESC
        LIMIT 1
    """, (order_id,))
    delivery = cursor.fetchone()

    cursor.close()
    conn.close()

    lines = []
    lines.append(f"CUSTOMER NAME: {cust_name}")
    lines.append(f"CUSTOMER EMAIL: {cust_email}")
    lines.append(f"CUSTOMER ADDRESS: {cust_address}")
    lines.append("")
    lines.append(f"Order Status: {order_status}")
    lines.append(f"Order Date: {created_at.strftime('%B %d, %Y') if created_at else 'N/A'}")
    lines.append(f"Total Amount: ${total_amount}")
    lines.append("")
    lines.append("ITEMS ORDERED:")
    for item in items:
        item_name, qty, price, is_available, stock = item
        availability = "In Stock" if is_available else "Out of Stock"
        lines.append(f"  - {item_name} x{qty} @ ${price} each ({availability})")
    lines.append("")

    if product_details:
        lines.append("PRODUCT DETAILS:")
        for row in product_details:
            prod_name, category, warranty, return_days, exchange = row
            if prod_name:
                lines.append(
                    f"  - {prod_name} ({category}): "
                    f"Warranty: {warranty or 'N/A'} | "
                    f"Returns: {return_days or 'N/A'} days"
                )
        lines.append("")

    if payment:
        pay_method, pay_status, pay_amount, paid_at = payment
        paid_str = paid_at.strftime('%B %d, %Y') if paid_at else 'Pending'
        lines.append(f"PAYMENT: {pay_method} | Status: {pay_status} | "
                     f"Amount: ${pay_amount} | Date: {paid_str}")
    else:
        lines.append("PAYMENT: No payment record found")
    lines.append("")

    if delivery:
        del_status, del_address, delivered_at, expected_date = delivery
        delivered_str = delivered_at.strftime('%B %d, %Y') if delivered_at else 'Not yet delivered'
        expected_str = expected_date.strftime('%B %d, %Y') if expected_date else 'Not available'
        lines.append(f"DELIVERY: Status: {del_status} | "
                     f"Address: {del_address} | "
                     f"Expected Delivery: {expected_str} | "
                     f"Delivered: {delivered_str}")
    else:
        lines.append("DELIVERY: No delivery record found")

    return "\n".join(lines)


def save_verified_order(call_sid, voice_code):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO call_verifications (call_sid, voice_code, verified_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (call_sid) DO UPDATE
        SET voice_code = %s, verified_at = %s
    """, (call_sid, voice_code, datetime.now(), voice_code, datetime.now()))
    conn.commit()
    cursor.close()
    conn.close()


def get_verified_order(call_sid):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT voice_code FROM call_verifications
        WHERE call_sid = %s AND verified_at IS NOT NULL
    """, (call_sid,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row[0] if row else None

def get_product_categories():
    """
    Fetch all distinct categories and their products from product_catalog.
    Returns a formatted string ready to inject into the LLM prompt.
    Automatically stays up to date as products are added or removed.
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT category, string_agg(product_name, ', ' ORDER BY product_name) AS products
        FROM product_catalog
        WHERE stock_available = true
        GROUP BY category
        ORDER BY category
    """)

    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    if not rows:
        return ""

    lines = ["CATEGORIES AND PRODUCTS WE CURRENTLY CARRY:"]
    for category, products in rows:
        lines.append(f"- {category}: {products}")

    return "\n".join(lines)

# Initialize database when imported
init_db()
