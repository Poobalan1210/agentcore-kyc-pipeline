

class TestDynamoKeysAreAttributeValues:
    """Keys must be AttributeValue-wrapped for the low-level DynamoDB client.

    Pins the pair of defects that made every reviewer decision return 500:
    `ParamValidationError: Invalid type for parameter Key.applicantId ... valid types: <class
    'dict'>`, on both the read (`load_case`) and the write (`review_decision_write`). Neither was
    ever hit because the console could not obtain a task token, so `Submit decision` stayed
    disabled and both lines were unreachable against the deployed stack — the bug hid behind
    another bug.
    """

    def _submission(self):
        from decisions import ReviewSubmission
        import inspect
        params = inspect.signature(ReviewSubmission).parameters
        kwargs = {}
        for name, p in params.items():
            if p.default is not inspect.Parameter.empty:
                continue
            kwargs[name] = "APP-1" if "applicant" in name else "x"
        return ReviewSubmission(**kwargs)

    def test_load_case_wraps_the_key(self):
        from decisions import TableKeySchema
        from review_handler import load_case

        seen = {}

        class Ddb:
            def get_item(self, **kw):
                seen.update(kw)
                return {}

        load_case(self._submission(), table_name="t", keys=TableKeySchema(), dynamodb=Ddb())
        assert seen["Key"]["applicantId"] == {"S": "APP-1"}, seen["Key"]
        assert seen["Key"]["sk"] == {"S": "CASE"}, seen["Key"]

    def test_every_key_value_is_a_single_member_dict(self):
        """Guards the shape generally, so a new key attribute cannot regress unnoticed."""
        from decisions import TableKeySchema
        from review_handler import load_case

        seen = {}

        class Ddb:
            def get_item(self, **kw):
                seen.update(kw)
                return {}

        load_case(self._submission(), table_name="t", keys=TableKeySchema(), dynamodb=Ddb())
        for attribute, value in seen["Key"].items():
            assert isinstance(value, dict) and len(value) == 1, (
                f"{attribute} must be an AttributeValue like {{'S': ...}}, got {value!r}"
            )


class TestCaseRecordIsDeserialized:
    """`decisions.py` is pure functions over a PLAIN case record, so the wire shape stops here.

    Pins the live 422: a submission whose caseId was byte-identical to the record was rejected
    `CASE_MISMATCH` because `str(case.get("caseId"))` saw `"{'S': 'CASE-...'}"`. Only reachable
    once a reviewer could obtain a task token at all, which is why it had never fired.
    """

    def test_scalars_are_unwrapped(self):
        from review_handler import deserialize_item
        out = deserialize_item({
            "caseId": {"S": "CASE-1"},
            "count": {"N": "3"},
            "flag": {"Bool": False},
            "other": {"BOOL": True},
            "nothing": {"NULL": True},
        })
        assert out == {"caseId": "CASE-1", "count": 3, "flag": False,
                       "other": True, "nothing": None}

    def test_nested_maps_and_lists(self):
        from review_handler import deserialize_item
        out = deserialize_item({
            "phase1": {"M": {"tier": {"S": "LOW"}, "review": {"NULL": True}}},
            "transitions": {"L": [{"S": "SUBMITTED"}, {"S": "AWAITING_REVIEW"}]},
        })
        assert out["phase1"] == {"tier": "LOW", "review": None}
        assert out["transitions"] == ["SUBMITTED", "AWAITING_REVIEW"]

    def test_already_plain_values_pass_through(self):
        """Never raise on an unexpected attribute: refusing the whole decision would be worse."""
        from review_handler import deserialize_item
        out = deserialize_item({"plain": "value", "weird": {"X": 1, "Y": 2}})
        assert out["plain"] == "value"
        assert out["weird"] == {"X": 1, "Y": 2}

    def test_case_id_compares_equal_after_deserialization(self):
        """The exact failure: the validator's comparison must now succeed."""
        from review_handler import deserialize_item
        case = deserialize_item({"caseId": {"S": "CASE-APP-1"}})
        assert str(case.get("caseId", "")) == "CASE-APP-1"
