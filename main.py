import sys
import datetime
import csv
import toml
import json
import tempfile
import os
import time
import requests
from decimal import Decimal
import dataclasses
from dataclasses import dataclass

config = toml.load("config.toml")
columns = config["csv"]["columns"]


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


payments = []

with open(sys.argv[1]) as csvfile:
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


results: dict[str, list[Payment]] = {
    "approved": [],
    "second_look": [],
    "ignore": [],
}

items = list(reversed(payments))
exit_loop = False
for idx, payment in enumerate(items):
    if exit_loop:
        break
    print()
    print()
    for rule in config["naming"]["rule"]:
        if not rule.get("payee_contains") is None:
            if rule.get("payee_contains").lower() in payment.payee.lower():
                print(rule)
                payment = dataclasses.replace(
                    payment,
                    payee_friendly=rule["result"].get("name", payment.payee),
                    category=rule["result"].get("category", payment.category),
                )
        if not rule.get("reference_contains") is None:
            if rule.get("reference_contains").lower() in payment.payee.lower():
                print(rule)
                payment = dataclasses.replace(
                    payment,
                    payee_friendly=rule["result"].get("name", payment.payee),
                    category=rule["result"].get("category", payment.category),
                )

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
            results["approved"].append(payment)
            print("added to the approved list")
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
            results["approved"].append(payment)
            print("added to the approved list")
            break
        elif entered == "j":
            results["second_look"].append(payment)
            print("added to the second look list")
            break
        elif entered == "x":
            results["ignore"].append(payment)
            print("added to the ignore list")
            break
        elif entered == "q":
            results["second_look"].extend(items[idx:])
            print("adding all remaining items to second look list")
            exit_loop = True
            break


# let's persist all lists first
def persist(items: list[Payment], path: str):
    print("dumping to ", path)
    with open(path, "w") as fh:
        json.dump([x.to_json() for x in items], fh, indent=2)
    print("done")


persist(results["approved"], f"{datetime.datetime.now().isoformat()}-approved.json")
persist(
    results["second_look"], f"{datetime.datetime.now().isoformat()}-second_look.json"
)
persist(results["ignore"], f"{datetime.datetime.now().isoformat()}-ignore.json")


def create_bill(payment: Payment):
    response = requests.post(
        f"{config['cospend']['domain']}/index.php/apps/cospend/api-priv/projects/{config['cospend']['project_name']}/bills",
        auth=(config["cospend"]["username"], config["cospend"]["password"]),
        json={
            "amount": payment.amount / 100,
            "what": payment.payee_friendly,
            "category": config["cospend"]["mapping"].get(payment.category, 0),
            "comment": payment.reference,
            "payed_for": config["cospend"]["payed_for"],
            "payer": config["cospend"]["payer"],
            "paymentmodeid": 0,  # TODO: always use transfer / move to config.toml
            "repeat": "n",
            "timestamp": datetime.datetime.combine(
                payment.date, datetime.datetime.min.time()
            ).timestamp(),
        },
    )
    print(response.status_code, response.text)


for payment in results["approved"]:
    print("create_bill", payment.payee)
    create_bill(payment)
