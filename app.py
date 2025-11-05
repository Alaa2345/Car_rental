# app.py
from flask import Flask, render_template, request, jsonify, redirect, url_for, abort
from flask_sqlalchemy import SQLAlchemy
import os, uuid
from datetime import datetime
import click

app = Flask(__name__)

# ---- Database config ----------------------------------------------------------
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "SQLALCHEMY_DATABASE_URI", "sqlite:///carrental.db"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# ---- Models -------------------------------------------------------------------
class Car(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    make = db.Column(db.String(80), nullable=False)
    model = db.Column(db.String(80), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    price_per_day = db.Column(db.Float, nullable=False)
    location = db.Column(db.String(120), nullable=False)
    available = db.Column(db.Boolean, default=True)

    def label(self):
        return f"{self.year} {self.make} {self.model}"

    def as_dict(self):
        return {
            "id": self.id,
            "make": self.make,
            "model": self.model,
            "year": self.year,
            "price_per_day": self.price_per_day,
            "location": self.location,
            "available": self.available,
        }

class Reservation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ref = db.Column(db.String(36), unique=True, nullable=False, index=True)
    car_id = db.Column(db.Integer, db.ForeignKey("car.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(200), nullable=False)
    pickup_date = db.Column(db.Date, nullable=False)
    dropoff_date = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    car = db.relationship("Car")

class Account(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    phone = db.Column(db.String(20))

# ---- Filtering helper (used by / and /cars) ----------------------------------
def apply_filters(query):
    from sqlalchemy import or_, cast, String
    q = request.args.get("q", "").strip()
    location = request.args.get("location", "").strip()
    min_price = request.args.get("min_price", type=float)
    max_price = request.args.get("max_price", type=float)
    available = request.args.get("available")

    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                Car.make.ilike(like),
                Car.model.ilike(like),
                cast(Car.year, String).ilike(like),
            )
        )
    if location:
        query = query.filter(Car.location.ilike(f"%{location}%"))
    if min_price is not None:
        query = query.filter(Car.price_per_day >= min_price)
    if max_price is not None:
        query = query.filter(Car.price_per_day <= max_price)
    if available not in (None, ""):
        val = available.lower()
        if val in ("1", "true", "yes", "y"):
            query = query.filter(Car.available.is_(True))
        elif val in ("0", "false", "no", "n"):
            query = query.filter(Car.available.is_(False))
    return query

# ---- Pages --------------------------------------------------------------------
@app.route("/")
def index():
    total = Car.query.count()
    cars = apply_filters(Car.query).order_by(Car.price_per_day.asc()).all()
    return render_template("index.html", total=total, cars=cars)

# Account Management Route
@app.route("/account", methods=["GET", "POST"])
def account():
    user = Account.query.first()  # in the future, this would be the logged-in user

    if not user:
        #flash("No user found.")
        return redirect(url_for("index"))

    if request.method == "POST":
        user.username = request.form["username"]
        user.email = request.form["email"]
        user.phone = request.form["phone"]
        db.session.commit()
       # flash("✔ Account updated successfully!", "success")

    total = Car.query.count() if 'Car' in globals() else 0
    return render_template("account.html", user=user, total=total)

# Reservation form (GET)
@app.get("/reserve")
def reserve_form():
    # optional ?car_id=...
    car_id = request.args.get("car_id", type=int)
    car = Car.query.get(car_id) if car_id else None
    cars = Car.query.order_by(Car.make, Car.model).all()
    return render_template("reserve.html", car=car, cars=cars)

# Reservation submit (POST)
@app.post("/reserve")
def reserve_submit():
    car_id = request.form.get("car_id", type=int)
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip()
    pickup = (request.form.get("pickup_date") or "").strip()
    dropoff = (request.form.get("dropoff_date") or "").strip()

    car = Car.query.get(car_id)
    if not car:
        abort(400, "Unknown car selected.")

    # Basic validation
    try:
        pickup_date = datetime.strptime(pickup, "%Y-%m-%d").date()
        dropoff_date = datetime.strptime(dropoff, "%Y-%m-%d").date()
    except ValueError:
        return render_template(
            "reserve.html",
            cars=Car.query.order_by(Car.make, Car.model).all(),
            car=car,
            error="Dates must be in YYYY-MM-DD format.",
            form=request.form,
        )

    if dropoff_date <= pickup_date:
        return render_template(
            "reserve.html",
            cars=Car.query.order_by(Car.make, Car.model).all(),
            car=car,
            error="Drop-off must be after pick-up.",
            form=request.form,
        )

    if not name or not email:
        return render_template(
            "reserve.html",
            cars=Car.query.order_by(Car.make, Car.model).all(),
            car=car,
            error="Name and email are required.",
            form=request.form,
        )

    if not car.available:
        return render_template(
            "reserve.html",
            cars=Car.query.order_by(Car.make, Car.model).all(),
            car=car,
            error="Sorry, this car is not available.",
            form=request.form,
        )

    # Create reservation
    ref = str(uuid.uuid4())
    r = Reservation(
        ref=ref,
        car_id=car.id,
        name=name,
        email=email,
        pickup_date=pickup_date,
        dropoff_date=dropoff_date,
    )
    # Mark car unavailable for this demo
    car.available = False
    db.session.add(r)
    db.session.commit()

    return redirect(url_for("confirm", ref=ref))

# Confirmation page
@app.get("/confirm/<ref>")
def confirm(ref):
    r = Reservation.query.filter_by(ref=ref).first_or_404()
    return render_template("confirm.html", r=r)

# ---- JSON API -----------------------------------------------------------------
@app.get("/cars")
def list_cars():
    cars = apply_filters(Car.query).order_by(Car.price_per_day.asc()).all()
    return jsonify([c.as_dict() for c in cars])

# ---- CLI: flask create / flask seed ------------------------------------------
@app.cli.command("resetdb")
def resetdb():
    """Drop & recreate tables, then reseed (all cars available)."""
    db.drop_all()
    db.create_all()
    sample = [
        Car(make="Toyota", model="Corolla", year=2020, price_per_day=45, location="Guelph", available=True),
        Car(make="Honda", model="Civic", year=2019, price_per_day=42, location="Kitchener", available=True),
        Car(make="Ford", model="Focus", year=2018, price_per_day=35, location="Hamilton", available=True),
        Car(make="Tesla", model="Model 3", year=2021, price_per_day=95, location="Waterloo", available=True),
	Car(make="Lamborghini", model="Urus", year=2022, price_per_day=480, location="Toronto", available=True),
	Car(make="McLaren", model="720S", year=2021, price_per_day=520, location="Los Angeles", available=True),
	Car(make="Ferrari", model="918", year=2020, price_per_day=650, location="Richmond Hill", available=True),
	Car(make="Porsche", model="911 GT3 RS", year=2023, price_per_day=575, location="Oakville", available=True),
	Car(make="BMW", model="M5", year=2022, price_per_day=310, location="Burlington", available=True),
	Car(make="Mercedes", model="SLS", year=2019, price_per_day=450, location="Hamilton", available=True),

    ]
    db.session.add_all(sample)
    db.session.commit()
    click.echo("✔ Database reset and reseeded (all available).")


@app.cli.command("create")
def create():
    db.create_all()
    click.echo("✔ Tables created")

@app.cli.command("seed")
def seed():
    if Car.query.count() > 0:
        click.echo("ℹ Already seeded; skipping")
        return
    sample = [
        Car(make="Toyota", model="Corolla", year=2020, price_per_day=45, location="Austin",  available=True),
        Car(make="Honda",  model="Civic",   year=2019, price_per_day=42, location="Dallas",  available=True),
        Car(make="Ford",   model="Focus",   year=2018, price_per_day=35, location="Austin",  available=False),
        Car(make="Tesla",  model="Model 3", year=2021, price_per_day=95, location="Houston", available=True),
    ]
    db.session.add_all(sample)
    db.session.commit()
    click.echo(f"✔ Seeded {len(sample)} cars")

if __name__ == "__main__":
    app.run(debug=True)