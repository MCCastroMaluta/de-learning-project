#!/usr/bin/env python3
"""
generate_daily_tickets.py
===========================
Generates one day's worth of tickets and ticket_lines (yesterday's date),
simulating daily business operations. Meant to run once a day via GitHub
Actions, inserting directly into Supabase.

Reuses the business logic from the original historical generator (payment
provider weighting, promo-day boost, larger baskets on promo days, a small
rate of unmappable payment_method_raw values), but:
- Generates a single day, not a full year.
- Does not use a fixed random seed: each run produces different data.
- Does not insert `loaded_at` (that column doesn't exist in Supabase —
  Airbyte's own extraction metadata replaces it).
- Connects to Supabase instead of local Postgres.
"""

import os
import random
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP

import psycopg2
from faker import Faker

fake = Faker("es_AR")
# No fixed seed on purpose: each run should produce different data.

DB_CONFIG = dict(
    host=os.environ["PGHOST"],
    port=os.environ.get("PGPORT", "5432"),
    dbname=os.environ.get("PGDATABASE", "postgres"),
    user=os.environ["PGUSER"],
    password=os.environ["PGPASSWORD"],
)

AVG_TICKETS_PER_STORE_PER_DAY = 45
PROMO_WEIGHT_BOOST = 7
BASKET_BOOST_ON_PROMO = 1.4
MISTYPED_RAW_RATE = 0.015

BASE_PROVIDER_WEIGHTS = {
    "CASH": 25, "DEBITO": 20, "CREDITO_VISA": 15, "CREDITO_MASTERCARD": 10,
    "TARJETA_NARANJA": 8, "MERCADO_PAGO": 6, "CARREFOUR_CREDITO": 4,
    "CARREFOUR_CUENTA_DIGITAL": 3, "PATAGONIA_CLASICA": 3, "PATAGONIA_PLUS": 2,
    "PATAGONIA_SINGULAR": 1, "CUENTA_DNI": 2, "CLUB_LANACION": 1, "MICRF_CLASICO": 1,
}

GARBLED_RAW_SAMPLES = ["efect", "TAJETA X", "tarjjeta", "pago???", "OTRO",
                        "tarj-sin-especificar", "credito ???", "pago app",
                        "efec.", "tarjeta banco"]


def money(x):
    return Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def get_connection():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    return conn


def insert_returning(cur, sql, params):
    cur.execute(sql, params)
    return cur.fetchone()[0]


def load_reference(cur):
    """Loads reference data that doesn't change day to day: stores, products,
    payment providers, the raw-text-to-provider mapping, and active promotions."""
    cur.execute("SELECT store_id FROM promo_ops.stores")
    store_ids = [r[0] for r in cur.fetchall()]

    cur.execute("SELECT product_id, category_id FROM promo_ops.products")
    products = [dict(product_id=r[0], category_id=r[1]) for r in cur.fetchall()]

    cur.execute("SELECT provider_id, provider_code FROM promo_ops.payment_providers")
    provider_id_by_code = {code: pid for pid, code in cur.fetchall()}

    cur.execute("SELECT raw_text, provider_id FROM promo_ops.payment_method_mapping")
    raw_by_provider = {}
    for raw_text, provider_id in cur.fetchall():
        raw_by_provider.setdefault(provider_id, []).append(raw_text)

    cur.execute(
        "SELECT provider_id, store_id, weekday, discount_pct, min_purchase_amount, "
        "max_discount_per_transaction, valid_from, valid_to FROM promo_ops.promotions WHERE is_active = TRUE"
    )
    promotions = []
    for row in cur.fetchall():
        promotions.append(dict(provider_id=row[0], store_id=row[1], weekday=row[2], discount_pct=row[3],
                                min_purchase=row[4], cap=row[5], valid_from=row[6], valid_to=row[7]))

    return dict(store_ids=store_ids, products=products, provider_id_by_code=provider_id_by_code,
                raw_by_provider=raw_by_provider, promotions=promotions)


def find_active_promotion(promotions, provider_id, store_id, day):
    """Finds the specific promotion (if any) that applies to this provider,
    store and weekday, used later to compute the discount actually given."""
    weekday = day.weekday()
    for promo in promotions:
        if promo["provider_id"] != provider_id:
            continue
        if promo["weekday"] != weekday:
            continue
        if promo["store_id"] is not None and promo["store_id"] != store_id:
            continue
        if not (promo["valid_from"] <= day <= (promo["valid_to"] or day)):
            continue
        return promo
    return None


def providers_with_active_promo_today(promotions, store_id, day):
    """Returns the set of provider_ids that have an active promotion today,
    used to boost their selection weight and basket size below."""
    weekday = day.weekday()
    active = set()
    for promo in promotions:
        if promo["weekday"] != weekday:
            continue
        if promo["store_id"] is not None and promo["store_id"] != store_id:
            continue
        if promo["valid_from"] <= day <= (promo["valid_to"] or day):
            active.add(promo["provider_id"])
    return active


def weighted_provider_choice(provider_id_by_code, active_provider_ids):
    """Picks a payment provider for this ticket. Providers with an active
    promo today get their weight multiplied by PROMO_WEIGHT_BOOST, without
    removing the other providers — people don't switch payment method
    entirely, they just lean toward the one with a promo more often."""
    codes, weights = [], []
    for code, weight in BASE_PROVIDER_WEIGHTS.items():
        pid = provider_id_by_code[code]
        w = weight * PROMO_WEIGHT_BOOST if pid in active_provider_ids else weight
        codes.append(code)
        weights.append(w)
    chosen_code = random.choices(codes, weights=weights, k=1)[0]
    return provider_id_by_code[chosen_code]


def get_price_map(products):
    """Stable base price per product for this run (not persisted between
    runs — a known simplification for this learning project)."""
    return {p["product_id"]: money(random.uniform(400, 9000)) for p in products}


def generate_day(cur, ref, price_map, day):
    """Generates and inserts all tickets and ticket_lines for a single day,
    across all stores."""
    total_tickets = 0
    total_mistyped = 0
    for store_id in ref["store_ids"]:
        active_provider_ids = providers_with_active_promo_today(ref["promotions"], store_id, day)
        num_tickets_today = max(5, int(random.gauss(AVG_TICKETS_PER_STORE_PER_DAY, 8)))

        for _ in range(num_tickets_today):
            hour = random.choices(population=list(range(9, 21)),
                                   weights=[3, 4, 5, 6, 8, 9, 7, 6, 8, 9, 7, 4])[0]
            ts = dt.datetime.combine(day, dt.time(hour, random.randint(0, 59)))

            provider_id = weighted_provider_choice(ref["provider_id_by_code"], active_provider_ids)
            on_promo_day_for_this_provider = provider_id in active_provider_ids

            # ~1.5% of tickets get a genuinely unmappable payment_method_raw,
            # on purpose — practice for fixing it in the staging layer later.
            is_mistyped = random.random() < MISTYPED_RAW_RATE
            if is_mistyped:
                payment_method_raw = random.choice(GARBLED_RAW_SAMPLES)
                total_mistyped += 1
            else:
                payment_method_raw = random.choice(ref["raw_by_provider"][provider_id])

            # Baskets tend to be a bit bigger on a promo day, to make better
            # use of the discount / reach the minimum purchase amount.
            n_items = random.randint(1, 8)
            if on_promo_day_for_this_provider and random.random() < 0.6:
                n_items = max(n_items, int(n_items * BASKET_BOOST_ON_PROMO))
            chosen_products = random.sample(ref["products"], k=min(n_items, len(ref["products"])))

            subtotal = Decimal("0")
            line_payload = []
            for p in chosen_products:
                qty = random.randint(1, 4)
                unit_price = price_map[p["product_id"]]
                line_total = money(unit_price * qty)
                subtotal += line_total
                line_payload.append(dict(product_id=p["product_id"], quantity=qty,
                                          unit_price=unit_price, line_total=line_total))

            discount = Decimal("0")
            if not is_mistyped:
                promo = find_active_promotion(ref["promotions"], provider_id, store_id, day)
                if promo and (promo["min_purchase"] is None or subtotal >= promo["min_purchase"]):
                    discount = money(subtotal * promo["discount_pct"] / 100)
                    if promo["cap"] is not None:
                        discount = min(discount, promo["cap"])

            total_amount = money(subtotal - discount)

            ticket_id = insert_returning(
                cur,
                "INSERT INTO tickets (store_id, ticket_timestamp, payment_method_raw, subtotal_amount, "
                "discount_amount, total_amount) VALUES (%s,%s,%s,%s,%s,%s) RETURNING ticket_id",
                (store_id, ts, payment_method_raw, subtotal, discount, total_amount),
            )
            for line in line_payload:
                cur.execute(
                    "INSERT INTO ticket_lines (ticket_id, product_id, quantity, unit_price, line_total) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (ticket_id, line["product_id"], line["quantity"], line["unit_price"], line["line_total"]),
                )
            total_tickets += 1

    return total_tickets, total_mistyped


def main():
    day = dt.date.today() - dt.timedelta(days=1)  # "yesterday": the business day that just closed

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SET search_path TO promo_ops")

    ref = load_reference(cur)
    price_map = get_price_map(ref["products"])

    try:
        total_tickets, total_mistyped = generate_day(cur, ref, price_map, day)
        conn.commit()
        print(f"✅ {day.isoformat()}: {total_tickets} tickets generated ({total_mistyped} with unmappable payment_method_raw).")
    except Exception:
        conn.rollback()
        print("❌ Error — transaction rolled back.")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()