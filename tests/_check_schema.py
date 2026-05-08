import psycopg2
conn = psycopg2.connect("host=127.0.0.1 port=5455 dbname=aiderm_cliniq user=postgres password=postgres")
cur = conn.cursor()
cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='verification_tokens' ORDER BY ordinal_position")
print("verification_tokens columns:", [r[0] for r in cur.fetchall()])
cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='verification_tokens' AND column_name ILIKE '%otp%'")
print("OTP-related columns:", [r[0] for r in cur.fetchall()])
conn.close()
