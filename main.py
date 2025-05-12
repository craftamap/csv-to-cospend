import typer
from typing_extensions import Annotated
from typing import Optional
import sys
import datetime
import csv
import tomllib
import json
import tempfile
import os
import time
import requests
from decimal import Decimal
import dataclasses
from dataclasses import dataclass
from pathlib import Path

with open("config.toml", "rb") as f:
    config = tomllib.load(f)

@dataclass
class Payment:
    date: datetime.date
    payee: str
    payee_friendly: str
    reference: str
    amount: int  # in cents
    category: str | None

    def to_json(self) -> dict:
        d = dataclasses.asdict(self)
        d["date"] = str(d["date"])
        return d

    @staticmethod
    def from_json(obj: dict):
        return Payment(
            date=datetime.datetime.strptime(obj["date"], "%Y-%m-%d").date(),
            payee=obj["payee"],
            payee_friendly=obj["payee_friendly"],
            reference=obj["reference"],
            amount=obj["amount"],
            category=obj["category"],
        )


app = typer.Typer()

def log_info(*args):
    print(" \033[92m::\033[0m", *args)
def log_error(*args):
    print(" \033[91m::\033[0m", *args)

def csv_to_payment(csv_path: Path) -> list[Payment]:
    columns = config["csv"]["columns"]
    payments = []

    with open(csv_path) as csvfile:
        reader = csv.DictReader(csvfile, delimiter=";")
        for row in reader:
            date = datetime.datetime.strptime(row[columns["date"]], "%d.%m.%Y").date()
            # replace commas with dots
            # parse the number using Decimal to not loose any data through double precision
            # take it times 100 to get to the cents
            # invert it for cospend and convert it to an int
            amount = int(-(Decimal(row[columns["amount"]].replace(",", ".")) * 100))

            payments.append(
                Payment(
                    date=date,
                    payee=row[columns["payee"]],
                    payee_friendly=row[columns["payee"]],
                    reference=row[columns["reference"]],
                    amount=amount,
                    category=None,
                )
            )
    return payments


def simplify_payment(payment: Payment) -> Payment:
    for rule in config["naming"]["rule"]:
        if not rule.get("payee_contains") is None:
            if rule.get("payee_contains").lower() in payment.payee.lower():
                payment = dataclasses.replace(
                    payment,
                    payee_friendly=rule["result"].get("name", payment.payee),
                    category=rule["result"].get("category", payment.category),
                )
        if not rule.get("reference_contains") is None:
            if rule.get("reference_contains").lower() in payment.payee.lower():
                payment = dataclasses.replace(
                    payment,
                    payee_friendly=rule["result"].get("name", payment.payee),
                    category=rule["result"].get("category", payment.category),
                )
    return payment


def persist(items: list[Payment], path: str):
    log_info("dumping to ", path)
    with open(path, "w") as fh:
        json.dump([x.to_json() for x in items], fh, indent=2)
    log_info("done")


def classify_payments(
    payments: list[Payment],
) -> (list[Payment], list[Payment], list[Payment]):
    approved: list[Payment] = []
    second_look: list[Payment] = []
    ignore: list[Payment] = []

    items = list(reversed(payments))
    exit_loop = False
    for idx, payment in enumerate(items):
        if exit_loop:
            break
        print()
        payment = simplify_payment(payment)

        print(f"{idx+1}/{len(payments)}")
        print(f"Date:       {payment.date.isoformat()}")
        print(f"Payee:      {payment.payee_friendly} ({payment.payee})")
        print(f"Reference:  {payment.reference}")
        print(f"Amount:     {payment.amount/100:.2f} €")

        while True:
            print(
                "(a) approved / (c) add category / (e) edit by hand and approve / (j) second look / (x) ignore / (q) quit"
            )
            entered = input()
            if entered == "a":
                approved.append(payment)
                log_info("added to the approved list")
                break
            if entered == "c":
                print("(g) grocery")
                print("(s) shopping")
                print("(x) dont change")
                categories = {
                    "g": "grocery",
                    "s": "shopping",
                }
                payment.category = categories.get(input())
            elif entered == "e":
                with tempfile.NamedTemporaryFile("w", delete=False) as tmpF:
                    js = json.dump(payment.to_json(), tmpF, indent=2)
                    path = tmpF.name
                os.system("%s %s" % (os.getenv("EDITOR"), path))
                with open(path) as tmpF:
                    payment = Payment.from_json(json.load(tmpF))
                approved.append(payment)
                log_info("added to the approved list")
                break
            elif entered == "j":
                second_look.append(payment)
                log_info("added to the second look list")
                break
            elif entered == "x":
                ignore.append(payment)
                log_info("added to the ignore list")
                break
            elif entered == "q":
                second_look.extend(items[idx:])
                print("adding all remaining items to second look list")
                exit_loop = True
                break
    return (approved, second_look, ignore)


@app.command()
def classify(csv_path: Annotated[Optional[Path], typer.Option()] = None, continue_with: Annotated[Optional[Path], typer.Option()] = None):
    if not csv_path is None:
        payments = csv_to_payment(csv_path)
    elif not continue_with is None:
        with open(continue_with) as tmpF:
            payments = list(reversed([Payment.from_json(x) for x in json.load(tmpF)]))
    (approved, second_look, ignore) = classify_payments(payments)

    persist(approved, f"{datetime.datetime.now().isoformat()}-approved.json")
    persist(second_look, f"{datetime.datetime.now().isoformat()}-second_look.json")
    persist(ignore, f"{datetime.datetime.now().isoformat()}-ignore.json")


def create_bill(payment: Payment):
    response = requests.post(
        f"{config['cospend']['domain']}/index.php/apps/cospend/api-priv/projects/{config['cospend']['project_name']}/bills",
        auth=(config["cospend"]["username"], config["cospend"]["password"]),
        json={
            "amount": payment.amount / 100,
            "what": payment.payee_friendly,
            "category": config["cospend"]["category_mapping"].get(payment.category, 0),
            "comment": payment.reference,
            "payed_for": config["cospend"]["payed_for"],
            "payer": int(config["cospend"]["payer"]),
            "paymentmodeid": 0,  # TODO: always use transfer / move to config.toml
            "repeat": "n",
            "timestamp": datetime.datetime.combine(
                payment.date, datetime.datetime.min.time()
            ).timestamp(),
        },
    )
    print(response.status_code, response.text)


@app.command()
def push(json_path: Annotated[Path, typer.Argument()]):
    with open(json_path) as tmpF:
        payments = [Payment.from_json(x) for x in json.load(tmpF)]
    for payment in payments:
        print(f"posting {payment}")
        create_bill(payment)


if __name__ == "__main__":
    app()
