from flask import Flask, request, jsonify
import os
from supabase import create_client, Client
from dotenv import load_dotenv
import uuid

load_dotenv()

app = Flask(__name__)

@app.before_request
def handle_options():
    if request.method == "OPTIONS":
        response = app.make_response(("", 204))
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization,X-Admin-Password"
        response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
        return response

@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization,X-Admin-Password"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
    return response

SUPABASE_URL     = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY     = os.environ.get("SUPABASE_KEY", "")
MAX_PER_BUS      = 36

def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)

# ─── DEBUG ────────────────────────────────────────────────
@app.route("/api/debug", methods=["GET"])
def debug():
    wave_url = os.environ.get("WAVE_PAYMENT_URL", "NON DEFINI")
    return jsonify({
        "WAVE_PAYMENT_URL": wave_url,
        "SUPABASE_URL": os.environ.get("SUPABASE_URL", "NON DEFINI")[:30] + "...",
        "BASE_URL": os.environ.get("BASE_URL", "NON DEFINI"),
    })

# ─── RÉSERVER ─────────────────────────────────────────────
@app.route("/api/reserve", methods=["POST"])
def reserve():
    data  = request.get_json()
    name  = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()

    if not name or not phone:
        return jsonify({"error": "Le nom et le numéro sont obligatoires."}), 400
    if len(phone.replace(" ", "").replace("-", "")) < 8:
        return jsonify({"error": "Numéro invalide."}), 400

    try:
        supabase = get_supabase()
        existing = supabase.table("reservations").select("id,status,token").eq("phone", phone).execute()

        for row in existing.data:
            if row["status"] == "confirmed":
                return jsonify({"error": "Ce numéro a déjà une place confirmée."}), 409
            if row["status"] == "pending":
                return jsonify({
                    "success": True,
                    "token": row["token"],
                    "message": "Réservation en attente existante."
                })

        token = str(uuid.uuid4())
        supabase.table("reservations").insert({
            "name": name, "phone": phone,
            "status": "pending", "token": token
        }).execute()

        return jsonify({"success": True, "token": token})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── CONFIRMER AVEC RÉFÉRENCE WAVE ────────────────────────
@app.route("/api/confirm", methods=["POST"])
def confirm():
    data      = request.get_json()
    token     = (data.get("token") or "").strip()
    wave_ref  = (data.get("wave_ref") or "").strip().upper()

    if not token:
        return jsonify({"error": "Token manquant."}), 400
    if not wave_ref or len(wave_ref) < 10:
        return jsonify({"error": "Veuillez entrer votre référence de transaction Wave."}), 400

    try:
        supabase = get_supabase()

        # Vérifier que cette référence Wave n'a pas déjà été utilisée
        ref_check = supabase.table("reservations").select("id,phone").eq("wave_ref", wave_ref).eq("status", "confirmed").execute()
        if ref_check.data:
            return jsonify({"error": "Cette référence Wave a déjà été utilisée."}), 409

        # Trouver la réservation
        result = supabase.table("reservations").select("*").eq("token", token).execute()
        if not result.data:
            return jsonify({"error": "Réservation introuvable."}), 404

        reservation = result.data[0]

        if reservation["status"] == "confirmed":
            return jsonify({"success": True, "reservation": {
                "name": reservation["name"], "phone": reservation["phone"],
                "bus_number": reservation["bus_number"], "seat_number": reservation["seat_number"]
            }})

        # Assigner bus et siège
        count_result = supabase.table("reservations").select("id", count="exact").eq("status", "confirmed").execute()
        total       = count_result.count or 0
        bus_number  = (total // MAX_PER_BUS) + 1
        seat_number = (total % MAX_PER_BUS) + 1

        updated = supabase.table("reservations").update({
            "status": "confirmed",
            "bus_number": bus_number,
            "seat_number": seat_number,
            "wave_ref": wave_ref
        }).eq("token", token).execute()

        r = updated.data[0]
        return jsonify({"success": True, "reservation": {
            "name": r["name"], "phone": r["phone"],
            "bus_number": r["bus_number"], "seat_number": r["seat_number"]
        }})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── LISTE ────────────────────────────────────────────────
@app.route("/api/reservations", methods=["GET"])
def get_reservations():
    try:
        supabase = get_supabase()
        result   = supabase.table("reservations").select("name,phone,bus_number,seat_number").eq("status", "confirmed").order("bus_number").order("seat_number").execute()
        buses = {}
        for r in result.data:
            bus = r["bus_number"]
            if bus not in buses:
                buses[bus] = []
            buses[bus].append({"name": r["name"], "phone": _mask_phone(r["phone"]), "seat": r["seat_number"]})
        return jsonify({
            "buses": [{"bus": k, "passengers": v, "count": len(v)} for k, v in sorted(buses.items())],
            "total": len(result.data), "max_per_bus": MAX_PER_BUS
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── STATS ────────────────────────────────────────────────
@app.route("/api/stats", methods=["GET"])
def get_stats():
    try:
        supabase     = get_supabase()
        count_result = supabase.table("reservations").select("id", count="exact").eq("status", "confirmed").execute()
        total        = count_result.count or 0
        return jsonify({
            "total_confirmed": total,
            "current_bus": (total // MAX_PER_BUS) + 1,
            "seats_taken_in_bus": total % MAX_PER_BUS,
            "seats_left_in_bus": MAX_PER_BUS - (total % MAX_PER_BUS),
            "max_per_bus": MAX_PER_BUS
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def _mask_phone(phone: str) -> str:
    if len(phone) <= 4: return phone
    return phone[:2] + "*" * (len(phone) - 4) + phone[-2:]

# ─── ADMIN - Liste complète ────────────────────────────────
@app.route("/api/admin/reservations", methods=["GET"])
def admin_reservations():
    password = request.headers.get("X-Admin-Password", "")
    if password != os.environ.get("ADMIN_PASSWORD", "admin123"):
        return jsonify({"error": "Non autorisé."}), 401

    try:
        supabase = get_supabase()
        result = supabase.table("reservations").select("*").order("created_at", desc=True).execute()
        return jsonify({"reservations": result.data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── ADMIN - Supprimer ────────────────────────────────────
@app.route("/api/admin/delete/<reservation_id>", methods=["DELETE"])
def admin_delete(reservation_id):
    password = request.headers.get("X-Admin-Password", "")
    if password != os.environ.get("ADMIN_PASSWORD", "admin123"):
        return jsonify({"error": "Non autorisé."}), 401

    try:
        supabase = get_supabase()
        supabase.table("reservations").delete().eq("id", reservation_id).execute()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True, port=5000)
