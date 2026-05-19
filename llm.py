from groq import Groq
from dotenv import load_dotenv
import os
import re
from datetime import date
import calendar

load_dotenv()

client = Groq(api_key=os.getenv("GROQ_API_KEY"))


def detect_sentiment(text: str) -> str:
    """Simple keyword-based sentiment detection."""
    text_lower = text.lower()

    angry_words = ["ridiculous", "useless", "terrible", "worst", "angry",
                   "furious", "unacceptable", "disgusting", "pathetic", "stupid"]
    frustrated_words = ["frustrated", "annoyed", "fed up", "tired", "again",
                        "still", "waiting", "long", "delay", "why"]
    worried_words = ["worried", "concern", "scared", "afraid", "lost",
                     "missing", "wrong", "problem", "issue", "help"]
    cancel_words = ["cancel", "refund", "return", "quit", "done", "leave"]

    if any(word in text_lower for word in angry_words):
        return "ANGRY"
    elif any(word in text_lower for word in cancel_words):
        return "THREATENING_TO_CANCEL"
    elif any(word in text_lower for word in frustrated_words):
        return "FRUSTRATED"
    elif any(word in text_lower for word in worried_words):
        return "WORRIED"
    else:
        return "NEUTRAL"


NUMBER_FORMAT_RULE = """
CRITICAL FORMATTING RULE: Whenever you mention any number sequence such as
an order ID, phone number, or reference number, ALWAYS space out each digit
individually. For example:
- Order ID 1001 → say "1 0 0 1"
- Phone 9876543210 → say "9 8 7 6 5 4 3 2 1 0"
- Zip 600035 → say "6 0 0 0 3 5"

NEVER space out the following — say them naturally:
- Prices ($49.99, $150)
- Quantities (3 items, 2 units)
- Years (2026, 2025)
- Dates and ordinals (May 10th, the 5th, 3rd of June)
- Days of the month (10th, 21st, 3rd)
- Delivery timeframes (in 2 days, 3 weeks)
- Addresses ("MG Road, Chennai, Tamil Nadu")
"""

PERSONALITY_RULES = """
PERSONALITY AND TONE:
- You are warm, empathetic, and genuinely helpful — not robotic or scripted
- Speak naturally like a real human support agent would on a phone call
- Use natural conversational phrases like "Of course", "Absolutely", "I understand"
- Never start two consecutive responses with the same word or phrase
- Vary your language — don't repeat the same phrases every turn
- Keep responses concise — this is a voice call, not a chat

EMOTIONAL INTELLIGENCE:
- Always acknowledge the customer's emotion BEFORE answering their question
- If frustrated: "I completely understand your frustration, and I'm going to help you right now."
- If angry: stay calm — "I sincerely apologise for this experience. Let me look into this immediately."
- If worried: "I can hear that this is concerning, and I want to sort this out together."
- If calm: be warm and friendly — match their energy
- Never be dismissive or robotic when emotions are high
- Never say "I cannot help with that" — always offer an alternative

HANDLING DIFFICULT SITUATIONS:
- If threatening to cancel: acknowledge, apologise, offer to escalate
- If harsh language: stay calm and professional
- If repeating a question: rephrase your answer differently
- If confused: slow down, simplify, guide step by step

WHAT TO NEVER DO:
- Never reveal you are an AI unless directly asked
- Never say "I cannot" without offering an alternative
- Never give the same response twice in a row
- Never ignore an emotional statement to jump to facts
- Never use jargon like "per our policy", "as per records", "kindly note"
"""

PRODUCT_QUERY_RULES_TEMPLATE = """
HANDLING PRODUCT-SPECIFIC QUERIES — VERY IMPORTANT:

When a customer asks about ANY of the following WITHOUT specifying a product:
- Promotions, offers, discounts, deals, sales
- Return or refund policies
- Warranty or guarantee information
- Exchange policies

STEP 1 — Ask them to specify first. Never list all products at once.
- "Of course! Which product or category are you asking about?"
- "Absolutely — which product are you interested in?"

STEP 2 — Once specified, answer ONLY for that product in ONE sentence maximum.
STEP 3 — If product already mentioned in their message, answer directly.

VOICE CALL RULES:
- NEVER list more than one product's offer in a single response
- NEVER read full policy descriptions — summarise in one short sentence
- Keep every response to a MAXIMUM of 2 sentences

EXAMPLES:
✅ "Do you have any offers?" → "Sure! Which category — smartphones, laptops, or headphones?"
✅ "MacBook Air return policy?" → Answer directly — product specified
❌ Never list all offers unprompted

{product_categories}
"""

# Fixed syntax — no longer references undefined dynamic_product_rules
SYSTEM_PROMPT_UNVERIFIED = (
    "You are Maya, a warm and professional customer support agent.\n"
    "This is a voice phone call — keep all responses under 2 sentences.\n"
    + PERSONALITY_RULES
    + NUMBER_FORMAT_RULE
    + "\nYou do not have the customer's order details yet.\n"
    "Greet the customer warmly and ask for their Order ID to proceed.\n"
    "Do not reveal or guess any order information until the Order ID is provided.\n"
    "If the customer seems frustrated before even giving their order ID, acknowledge it first.\n"
)


def build_product_query_rules(product_categories: str = "") -> str:
    """Fill the product_categories placeholder safely."""
    return PRODUCT_QUERY_RULES_TEMPLATE.format(
        product_categories=product_categories if product_categories
        else "Available categories: Smartphones, Laptops, Tablets, Headphones, Speakers, Smartwatches, Cameras"
    )


def needs_order_id(text: str) -> bool:
    """Returns True if the message requires an order ID to answer."""
    order_triggers = [
        "my order", "my delivery", "my package", "my item",
        "track", "tracking", "where is my", "when will my",
        "order status", "delivery status", "delivery date",
        "my payment", "my refund", "my return",
        "i ordered", "i purchased", "i bought",
        "hasn't arrived", "not delivered", "not received",
        "cancel my order", "change my order",
        "order number", "order id", "order details"
    ]
    text_lower = text.lower()
    return any(trigger in text_lower for trigger in order_triggers)


def extract_order_id(text):
    """Extract a numeric order ID — handles both digits and spoken words."""
    # First try direct digit match (4+ digits)
    matches = re.findall(r'\b(\d{4,10})\b', text)
    if matches:
        return int(matches[0])

    # Try converting spoken words to digits
    word_to_digit = {
        'zero': '0', 'oh': '0',
        'one': '1',
        'two': '2', 'to': '2', 'too': '2',
        'three': '3',
        'four': '4', 'for': '4',
        'five': '5',
        'six': '6',
        'seven': '7',
        'eight': '8',
        'nine': '9',
    }
    converted = text.lower()
    for word, digit in sorted(word_to_digit.items(), key=lambda x: -len(x[0])):
        converted = re.sub(r'\b' + word + r'\b', digit, converted)
    digits_only = re.sub(r'[^0-9]', '', converted)
    if len(digits_only) == 4:
        return int(digits_only)

    return None


def get_today_string():
    """Get today's date as a natural string."""
    today = date.today()
    day_name = calendar.day_name[today.weekday()]
    return f"{day_name}, {today.strftime('%B %d, %Y')}"


def build_additional_context(user_message):
    """
    Fetch relevant data based on what customer asked.
    Only injects data when product/category is specified.
    If not specified — returns empty so LLM asks first.
    """
    from database import (
        get_product_offers, get_return_policy,
        get_warranty, get_store_info
    )

    additional_context = ""
    msg_lower = user_message.lower()

    # ── Specific product names ──
    known_products = [
        "iphone", "samsung", "oneplus", "ipad", "macbook",
        "dell", "hp", "sony", "airpods", "jbl", "apple watch",
        "galaxy watch", "canon"
    ]

    # ── Category keywords → maps to database category names ──
    category_keywords = {
        "smartphone": "Smartphones",
        "smartphones": "Smartphones",
        "smart phone": "Smartphones",
        "smart ones": "Smartphones",
        "mobile": "Smartphones",
        "mobiles": "Smartphones",
        "phone": "Smartphones",
        "phones": "Smartphones",
        "laptop": "Laptops",
        "laptops": "Laptops",
        "notebook": "Laptops",
        "computer": "Laptops",
        "tablet": "Tablets",
        "tablets": "Tablets",
        "headphone": "Headphones",
        "headphones": "Headphones",
        "earphone": "Headphones",
        "earphones": "Headphones",
        "earbuds": "Headphones",
        "speaker": "Speakers",
        "speakers": "Speakers",
        "smartwatch": "Smartwatches",
        "smartwatches": "Smartwatches",
        "watch": "Smartwatches",
        "watches": "Smartwatches",
        "wearable": "Smartwatches",
        "camera": "Cameras",
        "cameras": "Cameras",
    }

    # Detect specific product name
    product_match = next((p for p in known_products if p in msg_lower), None)

    # Detect category keyword
    category_match = next(
        (category_keywords[k] for k in category_keywords if k in msg_lower),
        None
    )

    product_specified = product_match or category_match

    def fetch_offers():
        if product_match:
            return get_product_offers(product_name=product_match)
        elif category_match:
            return get_product_offers(category=category_match)
        return None

    def fetch_policy():
        if product_match:
            return get_return_policy(product_name=product_match)
        elif category_match:
            return get_return_policy(product_name=category_match)
        return None

    def fetch_warranty():
        if product_match:
            return get_warranty(product_name=product_match)
        elif category_match:
            return get_warranty(product_name=category_match)
        return None

    # ── Offers ──
    if any(w in msg_lower for w in ["offer", "discount", "deal", "promotion", "sale"]):
        if product_specified:
            data = fetch_offers()
            if data:
                lines = data.split("\n")
                additional_context += "\n\nREAL OFFER DATA — use ONLY this, mention max 1 offer:\n"
                additional_context += "\n".join(lines[:4])
            else:
                additional_context += (
                    f"\n\nOFFER DATA: No current offers found for "
                    f"'{product_match or category_match}'. "
                    f"Tell customer honestly no offers available for that product."
                )
        # If no product/category — return nothing, let LLM ask first

    # ── Return policy ──
    if any(w in msg_lower for w in ["return", "refund", "send back", "exchange"]):
        if product_specified:
            data = fetch_policy()
            if data:
                additional_context += f"\n\nREAL RETURN POLICY — use ONLY this:\n{data}"
            else:
                additional_context += (
                    f"\n\nRETURN POLICY: No policy found for "
                    f"'{product_match or category_match}'."
                )

    # ── Warranty ──
    if any(w in msg_lower for w in ["warranty", "guarantee", "repair", "damage"]):
        if product_specified:
            data = fetch_warranty()
            if data:
                additional_context += f"\n\nREAL WARRANTY DATA — use ONLY this:\n{data}"
            else:
                additional_context += (
                    f"\n\nWARRANTY: No warranty info found for "
                    f"'{product_match or category_match}'."
                )

    # ── Store info ──
    if any(w in msg_lower for w in
           ["store", "shop", "location", "branch", "timing", "open", "close"]):
        city = None
        known_cities = ["chennai", "mumbai", "bangalore", "delhi",
                        "kolkata", "hyderabad"]
        for city_name in known_cities:
            if city_name in msg_lower:
                city = city_name
                break
        data = get_store_info(city)
        if data:
            lines = data.split("\n")
            additional_context += "\n\nREAL STORE DATA — use ONLY this:\n"
            additional_context += "\n".join(lines[:4])

    return additional_context

def detect_unsupported_query(user_message, additional_context):
    """
    Detects if customer asked something outside available DB support.
    Returns True if unsupported.
    """

    msg = user_message.lower()

    supported_keywords = [
        "order", "delivery", "refund", "return",
        "warranty", "offer", "discount", "deal",
        "promotion", "store", "location", "timing",
        "product", "phone", "laptop", "tablet",
        "headphone", "speaker", "watch", "camera"
    ]

    # If query contains none of our supported topics
    if not any(k in msg for k in supported_keywords):
        return True

    # If topic is supported but no DB data found
    if not additional_context.strip():
        if any(
            k in msg for k in [
                "manual", "guide", "instructions",
                "troubleshoot", "setup", "specification",
                "specs", "configuration"
            ]
        ):
            return True

    return False

def chat(user_message, call_sid=None):
    from database import (
        get_conversation_history, save_message,
        get_order_context, get_order_context_cached,
        get_verified_order, save_verified_order,
        get_product_categories,
        redis_client
    )

    # ── Step 1: Save user message ──
    if call_sid:
        save_message(call_sid, "user", user_message)

    # ── Step 2: Get conversation history ──
    conversation_history = get_conversation_history(call_sid) if call_sid else []
    conversation_history = [m for m in conversation_history if m["role"] != "system"]

    if any(
        w in user_message.lower()
        for w in [
            "offer",
            "discount",
            "deal",
            "promotion",
            "sale"
        ]
    ):
        conversation_history = []

    # ── Step 3: Detect sentiment ──
    sentiment = detect_sentiment(user_message)
    sentiment_instruction = ""
    if sentiment != "NEUTRAL":
        sentiment_instruction = (
            f"\nIMPORTANT: Customer appears {sentiment}. "
            f"Acknowledge their emotion with empathy before answering.\n"
        )

    # ── Step 4: Build additional context ──
    additional_context = build_additional_context(user_message)

        # ── Unsupported query detection ──
    unsupported = detect_unsupported_query(
        user_message,
        additional_context
    )

    if unsupported and call_sid:

        counter_key = f"unsupported:{call_sid}"

        attempts = int(
            redis_client.get(counter_key) or 0
        )

        attempts += 1

        redis_client.setex(
            counter_key,
            1800,
            attempts
        )

        # Second unsupported query → escalate
        if attempts >= 2:
            print(f"[{call_sid}] Unsupported repeated — escalate")

            return "__TRANSFER_TO_HUMAN__"

        # First unsupported query → warning
        print(f"[{call_sid}] Unsupported query warning")

        warning = (
            "I'm your customer support agent and I can assist "
            "with order-related questions or available product support. "
            "How may I help you with that?"
        )

        if call_sid:
            save_message(call_sid, "assistant", warning)

        return warning

    # ── Step 5: Get live product categories for rules ──
    product_categories = get_product_categories()
    product_query_rules = build_product_query_rules(product_categories)

    # ── Step 6: Determine system prompt ──
    verified_order_id = get_verified_order(call_sid) if call_sid else None

    if verified_order_id:
        # ── VERIFIED — full order context ──
        order_context = get_order_context_cached(int(verified_order_id), call_sid)
        today_str = get_today_string()

        system_prompt = (
            "You are Maya, a warm and professional customer support agent.\n"
            "This is a voice phone call — keep all responses under 2 sentences.\n"
            + PERSONALITY_RULES
            + NUMBER_FORMAT_RULE
            + product_query_rules
            + f"""
TODAY'S DATE: {today_str}

Use today's date for relative time questions:
- If delivery date passed and not delivered → acknowledge delay empathetically
- Say dates naturally: "this Friday", "in 2 days", "tomorrow"
- If today IS the delivery date → say it should arrive today

CRITICAL DATA RULES — NEVER VIOLATE:
- ONLY use information explicitly provided below
- NEVER invent offers, policies, prices, product names, or order details
- If a customer asks about something not in the data below, say "I don't have that information right now"
- NEVER suggest checking the website as an alternative
- Every single fact you state must appear word-for-word in the data below

Never refer to phone number as order ID.
Never read out customer's phone number unless asked.

{order_context}
{additional_context}"""
        ) + sentiment_instruction

        print(f"[{call_sid}] Order: {verified_order_id} | Sentiment: {sentiment} | Today: {today_str}")

    else:
        order_id = extract_order_id(user_message)

        if order_id:
            order_context = get_order_context(int(order_id))

            if order_context:
                save_verified_order(call_sid, str(order_id))

                # Immediately cache in Redis so next turn picks it up
                get_order_context_cached(order_id, call_sid)

                today_str = get_today_string()

                system_prompt = (
                    "You are Maya, a warm and professional customer support agent.\n"
                    "Keep all responses under 2 sentences — this is a voice call.\n\n"
                    + PERSONALITY_RULES
                    + NUMBER_FORMAT_RULE
                    + "\nThe customer just provided their Order ID and it was found.\n"
                    "Greet them warmly by their first name and confirm their order is loaded.\n"
                    "Ask how you can help with their order.\n\n"
                    + order_context
                )
                print(f"[{call_sid}] Order {order_id} found and verified.")

            else:
                system_prompt = (
                    "You are Maya, a warm and professional customer support agent.\n"
                    "Keep all responses under 2 sentences — this is a voice call.\n\n"
                    "The customer provided an Order ID but it was not found.\n"
                    "Apologise warmly and ask them to double-check and try again."
                )
                print(f"[{call_sid}] Order ID {order_id} not found.")

        elif needs_order_id(user_message):
            system_prompt = (
                "You are Maya, a friendly and professional customer support agent.\n"
                "This is a voice phone call — keep all responses under 2 sentences.\n"
                + PERSONALITY_RULES
                + NUMBER_FORMAT_RULE
                + "\nThe customer needs help with something that requires their Order ID.\n"
                "Ask them to say their Order ID clearly, digit by digit.\n"
                "Do not ask for anything else yet."

                + """
                ABSOLUTE RULE — NO EXCEPTIONS:
                If the database section above shows "No current offers" or is empty for a product,
                say exactly: "We don't have any current offers for that product."
                NEVER suggest checking the website.
                NEVER make up offers, discounts, products, or policies.
                NEVER mention product names, prices, or specs not explicitly listed above.
                If you don't have data for something, say "I don't have that information available right now."
                """
            ) + sentiment_instruction
            print(f"[{call_sid}] Order ID required — prompting customer")

        else:
            # ── GENERAL — no order ID needed ──
            system_prompt = (
                "You are Maya, a friendly and professional customer support agent.\n"
                "This is a voice phone call — keep all responses under 2 sentences.\n"
                + PERSONALITY_RULES
                + NUMBER_FORMAT_RULE
                + product_query_rules
                + """
YOUR NAME: Maya
YOU ARE: A customer support agent

WHAT YOU CAN HELP WITH (no order ID needed):
- Current promotions, offers, discounts and deals
- Return and refund policies
- Warranty information
- Store locations and timings
- General product questions

WHAT REQUIRES AN ORDER ID:
- Order status and tracking
- Delivery updates
- Payment status
- Order-specific return requests

GREETING BEHAVIOUR:
- Introduce yourself warmly as Maya on the first turn
- Do NOT ask for order ID upfront — let the customer lead
- If customer asks about their order → ask for order ID naturally

CRITICAL DATA RULES:
- ONLY use information explicitly provided in this prompt
- NEVER invent offers, policies, or store details from memory
- If no data provided for a query → ask which product/category first

"""
                + (additional_context if additional_context else
                   "\nNo specific product data loaded yet. "
                   "If asked about offers or policies, ask which product/category first.")
            ) + sentiment_instruction

            print(f"[{call_sid}] General query | Sentiment: {sentiment}")

    # ── Step 7: Call LLM ──
    conversation_history.insert(0, {"role": "system", "content": system_prompt})
    conversation_history.append({"role": "user", "content": user_message})

    response = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=conversation_history,
        max_tokens=100,
        temperature=0.1
    )

    reply = response.choices[0].message.content

    # ── Step 8: Save reply ──
    if call_sid:
        save_message(call_sid, "assistant", reply)

    return reply


if __name__ == "__main__":
    print("Customer Support Agent Ready. Type 'quit' to exit.\n")
    while True:
        user_input = input("You: ")
        if user_input.lower() == "quit":
            break
        response = chat(user_input)
        print(f"Agent: {response}\n")
