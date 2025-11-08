# app.py
from flask import Flask, render_template, request, jsonify, redirect, url_for, abort
from flask import session, flash
from werkzeug.security import generate_password_hash, check_password_hash
from flask_sqlalchemy import SQLAlchemy
import os, uuid, stripe
from dotenv import load_dotenv
from datetime import datetime
import click

app = Flask(__name__)

# ---- Load environment variables from .env -----------------------------------
load_dotenv()  # make sure .env is in the same folder or provide path

# ---- Database config ----------------------------------------------------------
app.secret_key = os.getenv("SECRET_KEY", "car_rental_dev_key")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "SQLALCHEMY_DATABASE_URI", "sqlite:///carrental.db"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# ---- Stripe config ----------------------------------------------------------
# Load Stripe keys from environment
app.config["STRIPE_SECRET_KEY"] = os.getenv("STRIPE_SECRET_KEY")
app.config["STRIPE_PUBLIC_KEY"] = os.getenv("STRIPE_PUBLIC_KEY")

# Set the Stripe API key
stripe.api_key = app.config["STRIPE_SECRET_KEY"]

# Inject public key into templates
@app.context_processor
def inject_stripe_public_key():
    return dict(STRIPE_PUBLIC_KEY=app.config["STRIPE_PUBLIC_KEY"])

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
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), nullable=False) # NEW: Link to user account
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(200), nullable=False)
    pickup_date = db.Column(db.Date, nullable=False)
    dropoff_date = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(20), default='Confirmed') 
    paid = db.Column(db.Boolean, default=False)  # NEW: Tracks payment status
    payment_intent_id = db.Column(db.String(255), nullable=True) # NEW: Stripe payment intent ID

    car = db.relationship("Car")
    account = db.relationship("Account", backref="reservations")

class Account(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    phone = db.Column(db.String(20))
    password_hash = db.Column(db.String(128), nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

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

# ---- Email helper functions ---------------------------------------------------
def send_email(to, subject, body, email_type="general", amount=None, ref=None):
    """
    Simulates Stripe-style emails in the console for dev/test mode.
    email_type: 'payment', 'refund', or 'general'
    """
    header = "\n====== STRIPE EMAIL SIMULATION ======"
    footer = "=====================================\n"

    if email_type == "payment":
        subject = subject or "Payment Receipt"
        body = (
            f"Hi there,\n\n"
            f"We've successfully processed your payment.\n\n"
            f"Reservation Reference: {ref or 'N/A'}\n"
            f"Amount Charged: ${amount:.2f}\n\n"
            f"{body}\n"
            "Thank you for choosing our car rental service!\n"
        )

    elif email_type == "refund":
        subject = subject or "Refund Processed"
        body = (
            f"Hi there,\n\n"
            f"Your refund has been issued successfully.\n\n"
            f"Reservation Reference: {ref or 'N/A'}\n"
            f"Amount Refunded: ${amount:.2f}\n\n"
            f"{body}\n"
            "We're sorry to see you cancel, and we hope to serve you again!\n"
        )

    else:
        subject = subject or "Notification"
        body = f"{body}\n\nThank you!"

    print(f"{header}\nTo: {to}\nSubject: {subject}\n\n{body}\n{footer}")

# ---- Pages --------------------------------------------------------------------
@app.route("/")
def index():
    total = Car.query.count()
    cars = apply_filters(Car.query).order_by(Car.price_per_day.asc()).all()
    return render_template("index.html", total=total, cars=cars)

# ---- Payment routes ----------------------------------------------------------
@app.route("/create-checkout-session/<ref>", methods=["POST"])
def create_checkout_session(ref):
    reservation = Reservation.query.filter_by(ref=ref).first_or_404()

    # Prevent duplicate or invalid payments
    if reservation.status == "Cancelled":
        flash("Cannot pay for a cancelled reservation.", "error")
        return redirect(url_for("confirm", ref=ref))

    if getattr(reservation, "paid", False):
        flash("This reservation has already been paid.", "info")
        return redirect(url_for("confirm", ref=ref))

    try:
        # Calculate total amount in cents
        days = (reservation.dropoff_date - reservation.pickup_date).days
        amount_cents = int(reservation.car.price_per_day * days * 100)

        # Create Stripe checkout session
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "cad",
                    "product_data": {
                        "name": f"{reservation.car.label()} Rental",
                    },
                    "unit_amount": amount_cents,
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=url_for("payment_success", _external=True) + f"?ref={ref}&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=url_for("confirm", ref=ref, _external=True),
            customer_email=reservation.email,
        )

        # Save session id (optional, for tracking)
        reservation.payment_intent_id = checkout_session.payment_intent
        db.session.commit()

        return redirect(checkout_session.url, code=303)

    except Exception as e:
        flash(f"Error creating checkout session: {str(e)}", "error")
        return redirect(url_for("confirm", ref=ref))
    
@app.route("/payment/success")
def payment_success():
    ref = request.args.get("ref")
    session_id = request.args.get("session_id")
    reservation = Reservation.query.filter_by(ref=ref).first_or_404()

    if reservation.paid:
        flash("Payment already processed.", "info")
        return redirect(url_for("reservations_list"))

    try:
        # Retrieve the completed checkout session from Stripe
        checkout_session = stripe.checkout.Session.retrieve(session_id)

        # Save the PaymentIntent ID for potential refunds
        reservation.payment_intent_id = checkout_session.payment_intent
        reservation.status = "Confirmed"
        reservation.paid = True
        db.session.commit()

        # Calculate amount
        days = (reservation.dropoff_date - reservation.pickup_date).days
        amount = days * reservation.car.price_per_day

        # Simulate Stripe receipt email
        send_email(
            reservation.email,
            subject="Your Payment Was Successful",
            body=f"Your booking for {reservation.car.label()} from "
                 f"{reservation.pickup_date} to {reservation.dropoff_date} is confirmed.",
            email_type="payment",
            amount=amount,
            ref=reservation.ref
        )

        flash("Payment successful! Your reservation has been confirmed.", "success")

    except Exception as e:
        flash(f"Error processing payment confirmation: {str(e)}", "error")

    return redirect(url_for("reservations_list"))

@app.route("/reservation/<ref>/cancel", methods=["POST"])
def cancel_reservation(ref):
    reservation = Reservation.query.filter_by(ref=ref).first_or_404()

    if reservation.status == "Cancelled":
        flash("This reservation has already been cancelled.", "info")
        return redirect(url_for("reservations_list"))

    try:
        # Calculate amount for refund/email
        days = (reservation.dropoff_date - reservation.pickup_date).days
        amount = days * reservation.car.price_per_day

        if reservation.payment_intent_id:
            # Refund via Stripe
            refund = stripe.Refund.create(payment_intent=reservation.payment_intent_id)

            if refund.status != "succeeded":
                flash("Refund failed. Please contact support.", "error")
                return redirect(url_for("reservations_list"))

        # Update reservation and car only after successful refund or no payment
        reservation.status = "Cancelled"
        reservation.car.available = True
        db.session.commit()

        # Send simulated refund email
        send_email(
            reservation.email,
            subject="Your Refund Has Been Processed",
            body=f"Your booking for {reservation.car.label()} from "
                 f"{reservation.pickup_date} to {reservation.dropoff_date} has been cancelled successfully.",
            email_type="refund",
            amount=amount,
            ref=reservation.ref
        )

        flash("Reservation cancelled and refund processed.", "success")

    except Exception as e:
        flash(f"Error cancelling reservation: {str(e)}", "error")
        print(f"Error during cancellation: {e}", flush=True)

    return redirect(url_for("reservations_list"))

# ---- Login route --------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        user = Account.query.filter_by(username=username).first()

        if user and user.check_password(password):
            session["user_id"] = user.id
            flash("Logged in successfully!", "success")
            return redirect(url_for("account"))
        else:
            flash("Invalid username or password.", "error")

    return render_template("login.html")

# Logout route
@app.route("/logout")
def logout():
    session.pop("user_id", None)
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


# ---- Account management route ---------------------------------------------------------------
@app.route("/account", methods=["GET", "POST"])
def account():
    user_id = session.get("user_id")
    if not user_id:
        flash("Please log in to access your account.", "warning")
        return redirect(url_for("login"))

    user = Account.query.get(user_id)
    if not user:
        flash("User not found.", "error")
        return redirect(url_for("login"))

    if request.method == "POST":
        user.username = request.form["username"]
        user.email = request.form["email"]
        user.phone = request.form["phone"]
        db.session.commit()
        flash("Account updated successfully!", "success")

    total = Car.query.count()
    return render_template("account.html", user=user, total=total)

# ---- Password reset route ---------------------------------------------------------------
@app.route("/forgot", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email")
        user = Account.query.filter_by(email=email).first()

        if not user:
            flash("No account found with that email.", "error")
            return redirect(url_for("forgot_password"))

        session["reset_email"] = email
        flash("Please enter a new password.", "info")
        return redirect(url_for("reset_password"))

    total = Car.query.count()
    return render_template("forgot_password.html", total=total)

# Password reset form
@app.route("/reset", methods=["GET", "POST"])
def reset_password():
    email = session.get("reset_email")
    if not email:
        flash("Please start from the forgot password page.", "error")
        return redirect(url_for("forgot_password"))

    user = Account.query.filter_by(email=email).first()
    if not user:
        flash("Account not found.", "error")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        new_password = request.form.get("password")
        confirm = request.form.get("confirm_password")

        if new_password != confirm:
            flash("Passwords do not match.", "error")
            return redirect(url_for("reset_password"))

        user.set_password(new_password)
        db.session.commit()
        session.pop("reset_email", None)
        flash("Your password has been reset successfully!", "success")
        return redirect(url_for("login"))

    total = Car.query.count()
    return render_template("reset_password.html", total=total)

# Reservation form (GET)
@app.get("/reserve")
def reserve_form():
    if "user_id" not in session:
        flash("Please log in to reserve a car.", "warning")
        # redirect back here after login
        return redirect(url_for("login", next=request.url))
    
    car_id = request.args.get("car_id", type=int)
    car = Car.query.get(car_id) if car_id else None
    cars = Car.query.order_by(Car.make, Car.model).all()
    return render_template("reserve.html", car=car, cars=cars)

# Reservation submit (POST)
@app.post("/reserve")
def reserve_submit():
    if "user_id" not in session:
        flash("Please log in to reserve a car.", "warning")
        # redirect back here after login
        return redirect(url_for("login", next=request.url))
    
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
        account_id=session.get("user_id", 0)  # Link to user account if logged in
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

    # Calculate total rental amount
    days = (r.dropoff_date - r.pickup_date).days
    total_amount = r.car.price_per_day * days

    return render_template("confirm.html", r=r, total_amount=total_amount)

# ---- Modify reservation route ---------------------------------------------------------------
@app.route("/reservation/<ref>/modify", methods=["GET", "POST"])
def modify_reservation(ref):
    r = Reservation.query.filter_by(ref=ref).first_or_404()
    cars = Car.query.all()

    if r.status == "Cancelled":
        flash("You cannot modify a cancelled reservation.", "error")
        return redirect(url_for("confirm", ref=ref))


    if request.method == "POST":
        car_id = int(request.form["car_id"])

        # Convert string dates to date objects
        try:
            pickup_date = datetime.strptime(request.form["pickup_date"], "%Y-%m-%d").date()
            dropoff_date = datetime.strptime(request.form["dropoff_date"], "%Y-%m-%d").date()
        except ValueError:
            flash("Invalid date format. Please use YYYY-MM-DD.")
            return render_template("modify_reservation.html", r=r, cars=cars, error="Invalid date format")

        # Basic validation
        if pickup_date > dropoff_date:
            flash("Drop-off date must be after pick-up date.")
            return render_template("modify_reservation.html", r=r, cars=cars, error="Invalid dates")

        # If user changed car, make the old car available again
        if r.car_id != car_id:
            old_car = Car.query.get(r.car_id)
            if old_car:
                old_car.available = True

            new_car = Car.query.get(car_id)
            if not new_car.available and new_car.id != r.car_id:
                flash("Selected car is no longer available.")
                return render_template("modify_reservation.html", r=r, cars=cars, error="Car unavailable")
            new_car.available = False
            r.car_id = car_id

        # Update dates
        r.pickup_date = pickup_date
        r.dropoff_date = dropoff_date

        db.session.commit()
        flash("Reservation updated successfully.")

        send_email(
            r.email,
            "Your Reservation Has Been Updated",
            f"Hello {r.name},\n\nYour reservation ({r.ref}) has been updated:\n"
            f"Car: {r.car.label()}\n"
            f"Pickup: {r.pickup_date}\nDropoff: {r.dropoff_date}\n\nThank you."
        )

        return redirect(url_for("confirm", ref=ref))

    return render_template("modify_reservation.html", r=r, cars=cars)

# Reservations list on user account
@app.route("/reservations")
def reservations_list():
    user_id = session.get("user_id")

    if not user_id:
        flash("You must be logged in to view your reservations.")
        return render_template("reservations.html", reservations=[])

    reservations = (
        Reservation.query
        .filter_by(account_id=user_id)
        .order_by(Reservation.created_at.desc())
        .all()
    )

    return render_template("reservations.html", reservations=reservations)

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

@app.cli.command("create-demo-user")
def create_demo_user():
    """Create a demo login account."""
    with app.app_context():
        if Account.query.filter_by(username="demo").first():
            click.echo("Demo account already exists.")
        else:
            u = Account(username="demo", email="demo@email.com", phone="1234567890")
            u.set_password("password123")
            db.session.add(u)
            db.session.commit()
            click.echo("Demo account created (username: demo, password: password123).")

@app.context_processor
def inject_user():
    user_id = session.get("user_id")
    user = Account.query.get(user_id) if user_id else None
    return dict(current_user=user)

if __name__ == "__main__":
    app.run(debug=True)