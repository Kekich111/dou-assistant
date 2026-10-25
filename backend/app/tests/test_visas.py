from app.rules.visas import visa_block


def test_is_order():
    r = visa_block("Нужен приказ о вводе ИС в эксплуатацию")
    assert r and r["row_id"] == "internal_is_orders"
    print(r["required_visas"], r["additional_visas"])


def test_workgroup():
    r = visa_block("Нужен приказ о создании рабочей группы")
    print(r and r["row_id"])
    assert r is not None


def test_not_an_order():
    assert visa_block("Нужно ли сопроводительное письмо?") is None