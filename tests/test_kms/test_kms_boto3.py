import json
from datetime import datetime
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from unittest import mock
from dateutil.tz import tzutc
import base64
import os

import boto3
import botocore.exceptions
import sure  # noqa # pylint: disable=unused-import
from botocore.exceptions import ClientError
from freezegun import freeze_time
import pytest

from moto import mock_kms
from moto.core import DEFAULT_ACCOUNT_ID as ACCOUNT_ID


PLAINTEXT_VECTORS = [
    b"some encodeable plaintext",
    b"some unencodeable plaintext \xec\x8a\xcf\xb6r\xe9\xb5\xeb\xff\xa23\x16",
    "some unicode characters ø˚∆øˆˆ∆ßçøˆˆçßøˆ¨¥",
]


def _get_encoded_value(plaintext):
    if isinstance(plaintext, bytes):
        return plaintext

    return plaintext.encode("utf-8")


@mock_kms
def test_create_key_without_description():
    conn = boto3.client("kms", region_name="us-east-1")
    metadata = conn.create_key(Policy="my policy")["KeyMetadata"]

    metadata.should.have.key("AWSAccountId").equals(ACCOUNT_ID)
    metadata.should.have.key("KeyId")
    metadata.should.have.key("Arn")
    metadata.should.have.key("Description").equal("")


@mock_kms
def test_create_key():
    conn = boto3.client("kms", region_name="us-east-1")
    key = conn.create_key(
        Policy="my policy",
        Description="my key",
        KeyUsage="ENCRYPT_DECRYPT",
        Tags=[{"TagKey": "project", "TagValue": "moto"}],
    )

    key["KeyMetadata"]["Arn"].should.equal(
        f"arn:aws:kms:us-east-1:{ACCOUNT_ID}:key/{key['KeyMetadata']['KeyId']}"
    )
    key["KeyMetadata"]["AWSAccountId"].should.equal(ACCOUNT_ID)
    key["KeyMetadata"]["CreationDate"].should.be.a(datetime)
    key["KeyMetadata"]["CustomerMasterKeySpec"].should.equal("SYMMETRIC_DEFAULT")
    key["KeyMetadata"]["KeySpec"].should.equal("SYMMETRIC_DEFAULT")
    key["KeyMetadata"]["Description"].should.equal("my key")
    key["KeyMetadata"]["Enabled"].should.equal(True)
    key["KeyMetadata"]["EncryptionAlgorithms"].should.equal(["SYMMETRIC_DEFAULT"])
    key["KeyMetadata"]["KeyId"].should.match("[-a-zA-Z0-9]+")
    key["KeyMetadata"]["KeyManager"].should.equal("CUSTOMER")
    key["KeyMetadata"]["KeyState"].should.equal("Enabled")
    key["KeyMetadata"]["KeyUsage"].should.equal("ENCRYPT_DECRYPT")
    key["KeyMetadata"]["Origin"].should.equal("AWS_KMS")
    key["KeyMetadata"].should_not.have.key("SigningAlgorithms")

    key = conn.create_key(KeyUsage="ENCRYPT_DECRYPT", KeySpec="RSA_2048")

    sorted(key["KeyMetadata"]["EncryptionAlgorithms"]).should.equal(
        ["RSAES_OAEP_SHA_1", "RSAES_OAEP_SHA_256"]
    )
    key["KeyMetadata"].should_not.have.key("SigningAlgorithms")

    key = conn.create_key(KeyUsage="SIGN_VERIFY", KeySpec="RSA_2048")

    key["KeyMetadata"].should_not.have.key("EncryptionAlgorithms")
    sorted(key["KeyMetadata"]["SigningAlgorithms"]).should.equal(
        [
            "RSASSA_PKCS1_V1_5_SHA_256",
            "RSASSA_PKCS1_V1_5_SHA_384",
            "RSASSA_PKCS1_V1_5_SHA_512",
            "RSASSA_PSS_SHA_256",
            "RSASSA_PSS_SHA_384",
            "RSASSA_PSS_SHA_512",
        ]
    )

    key = conn.create_key(KeyUsage="SIGN_VERIFY", KeySpec="ECC_SECG_P256K1")

    key["KeyMetadata"].should_not.have.key("EncryptionAlgorithms")
    key["KeyMetadata"]["SigningAlgorithms"].should.equal(["ECDSA_SHA_256"])

    key = conn.create_key(KeyUsage="SIGN_VERIFY", KeySpec="ECC_NIST_P384")

    key["KeyMetadata"].should_not.have.key("EncryptionAlgorithms")
    key["KeyMetadata"]["SigningAlgorithms"].should.equal(["ECDSA_SHA_384"])

    key = conn.create_key(KeyUsage="SIGN_VERIFY", KeySpec="ECC_NIST_P521")

    key["KeyMetadata"].should_not.have.key("EncryptionAlgorithms")
    key["KeyMetadata"]["SigningAlgorithms"].should.equal(["ECDSA_SHA_512"])


@mock_kms
def test_create_multi_region_key():
    conn = boto3.client("kms", region_name="us-east-1")
    key = conn.create_key(
        Policy="my policy",
        Description="my key",
        KeyUsage="ENCRYPT_DECRYPT",
        MultiRegion=True,
        Tags=[{"TagKey": "project", "TagValue": "moto"}],
    )

    key["KeyMetadata"]["KeyId"].should.match("^mrk-")
    key["KeyMetadata"]["MultiRegion"].should.equal(True)


@mock_kms
def test_non_multi_region_keys_should_not_have_multi_region_properties():
    conn = boto3.client("kms", region_name="us-east-1")
    key = conn.create_key(
        Policy="my policy",
        Description="my key",
        KeyUsage="ENCRYPT_DECRYPT",
        MultiRegion=False,
        Tags=[{"TagKey": "project", "TagValue": "moto"}],
    )

    key["KeyMetadata"]["KeyId"].should_not.match("^mrk-")
    key["KeyMetadata"]["MultiRegion"].should.equal(False)


@mock_kms
def test_replicate_key():
    region_to_replicate_from = "us-east-1"
    region_to_replicate_to = "us-west-1"
    from_region_client = boto3.client("kms", region_name=region_to_replicate_from)
    to_region_client = boto3.client("kms", region_name=region_to_replicate_to)

    response = from_region_client.create_key(
        Policy="my policy",
        Description="my key",
        KeyUsage="ENCRYPT_DECRYPT",
        MultiRegion=True,
        Tags=[{"TagKey": "project", "TagValue": "moto"}],
    )
    key_id = response["KeyMetadata"]["KeyId"]

    with pytest.raises(to_region_client.exceptions.NotFoundException):
        to_region_client.describe_key(KeyId=key_id)

    with mock.patch.object(rsa, "generate_private_key", return_value=""):
        from_region_client.replicate_key(
            KeyId=key_id, ReplicaRegion=region_to_replicate_to
        )
    to_region_client.describe_key(KeyId=key_id)
    from_region_client.describe_key(KeyId=key_id)


@mock_kms
def test_create_key_deprecated_master_custom_key_spec():
    conn = boto3.client("kms", region_name="us-east-1")
    key = conn.create_key(KeyUsage="SIGN_VERIFY", CustomerMasterKeySpec="ECC_NIST_P521")

    key["KeyMetadata"].should_not.have.key("EncryptionAlgorithms")
    key["KeyMetadata"]["SigningAlgorithms"].should.equal(["ECDSA_SHA_512"])

    key["KeyMetadata"]["CustomerMasterKeySpec"].should.equal("ECC_NIST_P521")
    key["KeyMetadata"]["KeySpec"].should.equal("ECC_NIST_P521")


@pytest.mark.parametrize("id_or_arn", ["KeyId", "Arn"])
@mock_kms
def test_describe_key(id_or_arn):
    client = boto3.client("kms", region_name="us-east-1")
    response = client.create_key(Description="my key", KeyUsage="ENCRYPT_DECRYPT")
    key_id = response["KeyMetadata"][id_or_arn]

    response = client.describe_key(KeyId=key_id)

    response["KeyMetadata"]["AWSAccountId"].should.equal("123456789012")
    response["KeyMetadata"]["CreationDate"].should.be.a(datetime)
    response["KeyMetadata"]["CustomerMasterKeySpec"].should.equal("SYMMETRIC_DEFAULT")
    response["KeyMetadata"]["KeySpec"].should.equal("SYMMETRIC_DEFAULT")
    response["KeyMetadata"]["Description"].should.equal("my key")
    response["KeyMetadata"]["Enabled"].should.equal(True)
    response["KeyMetadata"]["EncryptionAlgorithms"].should.equal(["SYMMETRIC_DEFAULT"])
    response["KeyMetadata"]["KeyId"].should.match("[-a-zA-Z0-9]+")
    response["KeyMetadata"]["KeyManager"].should.equal("CUSTOMER")
    response["KeyMetadata"]["KeyState"].should.equal("Enabled")
    response["KeyMetadata"]["KeyUsage"].should.equal("ENCRYPT_DECRYPT")
    response["KeyMetadata"]["Origin"].should.equal("AWS_KMS")
    response["KeyMetadata"].should_not.have.key("SigningAlgorithms")


@mock_kms
def test_get_key_policy_default():
    # given
    client = boto3.client("kms", region_name="us-east-1")
    key_id = create_simple_key(client)

    # when
    policy = client.get_key_policy(KeyId=key_id, PolicyName="default")["Policy"]

    # then
    json.loads(policy).should.equal(
        {
            "Version": "2012-10-17",
            "Id": "key-default-1",
            "Statement": [
                {
                    "Sid": "Enable IAM User Permissions",
                    "Effect": "Allow",
                    "Principal": {"AWS": f"arn:aws:iam::{ACCOUNT_ID}:root"},
                    "Action": "kms:*",
                    "Resource": "*",
                }
            ],
        }
    )


@mock_kms
def test_describe_key_via_alias():
    client = boto3.client("kms", region_name="us-east-1")
    key_id = create_simple_key(client, description="my key")

    client.create_alias(AliasName="alias/my-alias", TargetKeyId=key_id)

    alias_key = client.describe_key(KeyId="alias/my-alias")
    alias_key["KeyMetadata"]["Description"].should.equal("my key")


@mock_kms
def test__create_alias__can_create_multiple_aliases_for_same_key_id():
    client = boto3.client("kms", region_name="us-east-1")
    key_id = create_simple_key(client)

    alias_names = ["alias/al1", "alias/al2", "alias/al3"]
    for name in alias_names:
        client.create_alias(AliasName=name, TargetKeyId=key_id)

    aliases = client.list_aliases(KeyId=key_id)["Aliases"]

    for name in alias_names:
        alias_arn = f"arn:aws:kms:us-east-1:{ACCOUNT_ID}:{name}"
        aliases.should.contain(
            {"AliasName": name, "AliasArn": alias_arn, "TargetKeyId": key_id}
        )


@mock_kms
def test_list_aliases():
    region = "us-west-1"
    client = boto3.client("kms", region_name=region)
    create_simple_key(client)

    default_alias_target_keys = {
        "aws/ebs": "7adeb491-68c9-4a5b-86ec-a86ce5364094",
        "aws/s3": "8c3faf07-f43c-4d11-abdb-9183079214c7",
        "aws/redshift": "dcdae9aa-593a-4e0b-9153-37325591901f",
        "aws/rds": "f5f30938-abed-41a2-a0f6-5482d02a2489",
    }
    default_alias_names = list(default_alias_target_keys.keys())

    aliases = client.list_aliases()["Aliases"]
    aliases.should.have.length_of(14)
    for name in default_alias_names:
        full_name = f"alias/{name}"
        arn = f"arn:aws:kms:{region}:{ACCOUNT_ID}:{full_name}"
        target_key_id = default_alias_target_keys[name]
        aliases.should.contain(
            {"AliasName": full_name, "AliasArn": arn, "TargetKeyId": target_key_id}
        )


@mock_kms
def test_list_aliases_for_key_id():
    region = "us-west-1"
    client = boto3.client("kms", region_name=region)

    my_alias = "alias/my-alias"
    alias_arn = f"arn:aws:kms:{region}:{ACCOUNT_ID}:{my_alias}"
    key_id = create_simple_key(client, description="my key")
    client.create_alias(AliasName=my_alias, TargetKeyId=key_id)

    aliases = client.list_aliases(KeyId=key_id)["Aliases"]
    aliases.should.have.length_of(1)
    aliases.should.contain(
        {"AliasName": my_alias, "AliasArn": alias_arn, "TargetKeyId": key_id}
    )


@mock_kms
def test_list_aliases_for_key_arn():
    region = "us-west-1"
    client = boto3.client("kms", region_name=region)
    key = client.create_key()
    key_id = key["KeyMetadata"]["KeyId"]
    key_arn = key["KeyMetadata"]["Arn"]

    id_alias = "alias/my-alias-1"
    client.create_alias(AliasName=id_alias, TargetKeyId=key_id)
    arn_alias = "alias/my-alias-2"
    client.create_alias(AliasName=arn_alias, TargetKeyId=key_arn)

    aliases = client.list_aliases(KeyId=key_arn)["Aliases"]
    aliases.should.have.length_of(2)
    for alias in [id_alias, arn_alias]:
        alias_arn = f"arn:aws:kms:{region}:{ACCOUNT_ID}:{alias}"
        aliases.should.contain(
            {"AliasName": alias, "AliasArn": alias_arn, "TargetKeyId": key_id}
        )


@pytest.mark.parametrize(
    "key_id",
    [
        "alias/does-not-exist",
        "arn:aws:kms:us-east-1:012345678912:alias/does-not-exist",
        "invalid",
    ],
)
@mock_kms
def test_describe_key_via_alias_invalid_alias(key_id):
    client = boto3.client("kms", region_name="us-east-1")

    with pytest.raises(client.exceptions.NotFoundException):
        client.describe_key(KeyId=key_id)


@mock_kms
def test_list_keys():
    client = boto3.client("kms", region_name="us-east-1")
    with mock.patch.object(rsa, "generate_private_key", return_value=""):
        k1 = client.create_key(Description="key1")["KeyMetadata"]
        k2 = client.create_key(Description="key2")["KeyMetadata"]

    keys = client.list_keys()["Keys"]
    keys.should.have.length_of(2)
    keys.should.contain({"KeyId": k1["KeyId"], "KeyArn": k1["Arn"]})
    keys.should.contain({"KeyId": k2["KeyId"], "KeyArn": k2["Arn"]})


@pytest.mark.parametrize("id_or_arn", ["KeyId", "Arn"])
@mock_kms
def test_enable_key_rotation(id_or_arn):
    client = boto3.client("kms", region_name="us-east-1")
    key_id = create_simple_key(client, id_or_arn=id_or_arn)

    client.get_key_rotation_status(KeyId=key_id)["KeyRotationEnabled"].should.equal(
        False
    )

    client.enable_key_rotation(KeyId=key_id)
    client.get_key_rotation_status(KeyId=key_id)["KeyRotationEnabled"].should.equal(
        True
    )

    client.disable_key_rotation(KeyId=key_id)
    client.get_key_rotation_status(KeyId=key_id)["KeyRotationEnabled"].should.equal(
        False
    )


@mock_kms
def test_enable_key_rotation_with_alias_name_should_fail():
    client = boto3.client("kms", region_name="us-east-1")
    key_id = create_simple_key(client)

    client.create_alias(AliasName="alias/my-alias", TargetKeyId=key_id)
    with pytest.raises(ClientError) as ex:
        client.enable_key_rotation(KeyId="alias/my-alias")
    err = ex.value.response["Error"]
    err["Code"].should.equal("NotFoundException")
    err["Message"].should.equal("Invalid keyId alias/my-alias")


@mock_kms
def test_generate_data_key():
    kms = boto3.client("kms", region_name="us-west-2")

    key = kms.create_key()
    key_id = key["KeyMetadata"]["KeyId"]
    key_arn = key["KeyMetadata"]["Arn"]

    response = kms.generate_data_key(KeyId=key_id, NumberOfBytes=32)

    # CiphertextBlob must NOT be base64-encoded
    with pytest.raises(Exception):
        base64.b64decode(response["CiphertextBlob"], validate=True)
    # Plaintext must NOT be base64-encoded
    with pytest.raises(Exception):
        base64.b64decode(response["Plaintext"], validate=True)

    response["KeyId"].should.equal(key_arn)


@pytest.mark.parametrize(
    "key_id",
    [
        "not-a-uuid",
        "alias/DoesNotExist",
        "arn:aws:kms:us-east-1:012345678912:alias/DoesNotExist",
        "d25652e4-d2d2-49f7-929a-671ccda580c6",
        "arn:aws:kms:us-east-1:012345678912:key/d25652e4-d2d2-49f7-929a-671ccda580c6",
    ],
)
@mock_kms
def test_invalid_key_ids(key_id):
    client = boto3.client("kms", region_name="us-east-1")

    with pytest.raises(client.exceptions.NotFoundException):
        client.generate_data_key(KeyId=key_id, NumberOfBytes=5)


@mock_kms
def test_disable_key():
    client = boto3.client("kms", region_name="us-east-1")
    key_id = create_simple_key(client)
    client.disable_key(KeyId=key_id)

    result = client.describe_key(KeyId=key_id)
    assert result["KeyMetadata"]["Enabled"] is False
    assert result["KeyMetadata"]["KeyState"] == "Disabled"


@mock_kms
def test_enable_key():
    client = boto3.client("kms", region_name="us-east-1")
    key_id = create_simple_key(client)
    client.disable_key(KeyId=key_id)
    client.enable_key(KeyId=key_id)

    result = client.describe_key(KeyId=key_id)
    assert result["KeyMetadata"]["Enabled"] is True
    assert result["KeyMetadata"]["KeyState"] == "Enabled"


@mock_kms
def test_schedule_key_deletion():
    client = boto3.client("kms", region_name="us-east-1")
    key_id = create_simple_key(client)
    if os.environ.get("TEST_SERVER_MODE", "false").lower() == "false":
        with freeze_time("2015-01-01 12:00:00"):
            response = client.schedule_key_deletion(KeyId=key_id)
            assert response["KeyId"] == key_id
            assert response["DeletionDate"] == datetime(
                2015, 1, 31, 12, 0, tzinfo=tzutc()
            )
    else:
        # Can't manipulate time in server mode
        response = client.schedule_key_deletion(KeyId=key_id)
        assert response["KeyId"] == key_id

    result = client.describe_key(KeyId=key_id)
    assert result["KeyMetadata"]["Enabled"] is False
    assert result["KeyMetadata"]["KeyState"] == "PendingDeletion"
    assert "DeletionDate" in result["KeyMetadata"]


@mock_kms
def test_schedule_key_deletion_custom():
    client = boto3.client("kms", region_name="us-east-1")
    key = client.create_key(Description="schedule-key-deletion")
    if os.environ.get("TEST_SERVER_MODE", "false").lower() == "false":
        with freeze_time("2015-01-01 12:00:00"):
            response = client.schedule_key_deletion(
                KeyId=key["KeyMetadata"]["KeyId"], PendingWindowInDays=7
            )
            assert response["KeyId"] == key["KeyMetadata"]["KeyId"]
            assert response["DeletionDate"] == datetime(
                2015, 1, 8, 12, 0, tzinfo=tzutc()
            )
    else:
        # Can't manipulate time in server mode
        response = client.schedule_key_deletion(
            KeyId=key["KeyMetadata"]["KeyId"], PendingWindowInDays=7
        )
        assert response["KeyId"] == key["KeyMetadata"]["KeyId"]

    result = client.describe_key(KeyId=key["KeyMetadata"]["KeyId"])
    assert result["KeyMetadata"]["Enabled"] is False
    assert result["KeyMetadata"]["KeyState"] == "PendingDeletion"
    assert "DeletionDate" in result["KeyMetadata"]


@mock_kms
def test_cancel_key_deletion():
    client = boto3.client("kms", region_name="us-east-1")
    key = client.create_key(Description="cancel-key-deletion")
    client.schedule_key_deletion(KeyId=key["KeyMetadata"]["KeyId"])
    response = client.cancel_key_deletion(KeyId=key["KeyMetadata"]["KeyId"])
    assert response["KeyId"] == key["KeyMetadata"]["KeyId"]

    result = client.describe_key(KeyId=key["KeyMetadata"]["KeyId"])
    assert result["KeyMetadata"]["Enabled"] is False
    assert result["KeyMetadata"]["KeyState"] == "Disabled"
    assert "DeletionDate" not in result["KeyMetadata"]


@mock_kms
def test_update_key_description():
    client = boto3.client("kms", region_name="us-east-1")
    key_id = create_simple_key(client)

    result = client.update_key_description(KeyId=key_id, Description="new_description")
    assert "ResponseMetadata" in result


@mock_kms
def test_tag_resource():
    client = boto3.client("kms", region_name="us-east-1")
    key = client.create_key(Description="cancel-key-deletion")
    response = client.schedule_key_deletion(KeyId=key["KeyMetadata"]["KeyId"])

    keyid = response["KeyId"]
    response = client.tag_resource(
        KeyId=keyid, Tags=[{"TagKey": "string", "TagValue": "string"}]
    )

    # Shouldn't have any data, just header
    assert len(response.keys()) == 1


@mock_kms
def test_list_resource_tags():
    client = boto3.client("kms", region_name="us-east-1")
    key = client.create_key(Description="cancel-key-deletion")
    response = client.schedule_key_deletion(KeyId=key["KeyMetadata"]["KeyId"])

    keyid = response["KeyId"]
    response = client.tag_resource(
        KeyId=keyid, Tags=[{"TagKey": "string", "TagValue": "string"}]
    )

    response = client.list_resource_tags(KeyId=keyid)
    assert response["Tags"][0]["TagKey"] == "string"
    assert response["Tags"][0]["TagValue"] == "string"


@mock_kms
def test_list_resource_tags_with_arn():
    client = boto3.client("kms", region_name="us-east-1")
    key = client.create_key(Description="cancel-key-deletion")
    client.schedule_key_deletion(KeyId=key["KeyMetadata"]["KeyId"])

    keyid = key["KeyMetadata"]["Arn"]
    client.tag_resource(KeyId=keyid, Tags=[{"TagKey": "string", "TagValue": "string"}])

    response = client.list_resource_tags(KeyId=keyid)
    assert response["Tags"][0]["TagKey"] == "string"
    assert response["Tags"][0]["TagValue"] == "string"


@mock_kms
def test_unknown_tag_methods():
    client = boto3.client("kms", region_name="us-east-1")

    with pytest.raises(ClientError) as ex:
        client.tag_resource(KeyId="unknown", Tags=[])
    err = ex.value.response["Error"]
    err["Message"].should.equal("Invalid keyId unknown")
    err["Code"].should.equal("NotFoundException")

    with pytest.raises(ClientError) as ex:
        client.untag_resource(KeyId="unknown", TagKeys=[])
    err = ex.value.response["Error"]
    err["Message"].should.equal("Invalid keyId unknown")
    err["Code"].should.equal("NotFoundException")

    with pytest.raises(ClientError) as ex:
        client.list_resource_tags(KeyId="unknown")
    err = ex.value.response["Error"]
    err["Message"].should.equal("Invalid keyId unknown")
    err["Code"].should.equal("NotFoundException")


@mock_kms
def test_list_resource_tags_after_untagging():
    client = boto3.client("kms", region_name="us-east-1")
    key_id = create_simple_key(client)

    client.tag_resource(
        KeyId=key_id,
        Tags=[
            {"TagKey": "key1", "TagValue": "s1"},
            {"TagKey": "key2", "TagValue": "s2"},
        ],
    )

    client.untag_resource(KeyId=key_id, TagKeys=["key2"])

    tags = client.list_resource_tags(KeyId=key_id)["Tags"]
    tags.should.equal([{"TagKey": "key1", "TagValue": "s1"}])


@pytest.mark.parametrize(
    "kwargs,expected_key_length",
    (
        (dict(KeySpec="AES_256"), 32),
        (dict(KeySpec="AES_128"), 16),
        (dict(NumberOfBytes=64), 64),
        (dict(NumberOfBytes=1), 1),
        (dict(NumberOfBytes=1024), 1024),
    ),
)
@mock_kms
def test_generate_data_key_sizes(kwargs, expected_key_length):
    client = boto3.client("kms", region_name="us-east-1")
    key_id = create_simple_key(client)

    response = client.generate_data_key(KeyId=key_id, **kwargs)

    assert len(response["Plaintext"]) == expected_key_length


@mock_kms
def test_generate_data_key_decrypt():
    client = boto3.client("kms", region_name="us-east-1")
    key = client.create_key(Description="generate-data-key-decrypt")

    resp1 = client.generate_data_key(
        KeyId=key["KeyMetadata"]["KeyId"], KeySpec="AES_256"
    )
    resp2 = client.decrypt(CiphertextBlob=resp1["CiphertextBlob"])

    assert resp1["Plaintext"] == resp2["Plaintext"]


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(KeySpec="AES_257"),
        dict(KeySpec="AES_128", NumberOfBytes=16),
        dict(NumberOfBytes=2048),
        dict(),
    ],
)
@mock_kms
def test_generate_data_key_invalid_size_params(kwargs):
    client = boto3.client("kms", region_name="us-east-1")
    key = client.create_key(Description="generate-data-key-size")

    with pytest.raises(botocore.exceptions.ClientError):
        client.generate_data_key(KeyId=key["KeyMetadata"]["KeyId"], **kwargs)


@pytest.mark.parametrize(
    "key_id",
    [
        "alias/DoesNotExist",
        "arn:aws:kms:us-east-1:012345678912:alias/DoesNotExist",
        "d25652e4-d2d2-49f7-929a-671ccda580c6",
        "arn:aws:kms:us-east-1:012345678912:key/d25652e4-d2d2-49f7-929a-671ccda580c6",
    ],
)
@mock_kms
def test_generate_data_key_invalid_key(key_id):
    client = boto3.client("kms", region_name="us-east-1")

    with pytest.raises(client.exceptions.NotFoundException):
        client.generate_data_key(KeyId=key_id, KeySpec="AES_256")


@pytest.mark.parametrize(
    "prefix,append_key_id",
    [
        ("alias/DoesExist", False),
        ("arn:aws:kms:us-east-1:012345678912:alias/DoesExist", False),
        ("", True),
        ("arn:aws:kms:us-east-1:012345678912:key/", True),
    ],
)
@mock_kms
def test_generate_data_key_all_valid_key_ids(prefix, append_key_id):
    client = boto3.client("kms", region_name="us-east-1")
    key = client.create_key()
    key_id = key["KeyMetadata"]["KeyId"]
    client.create_alias(AliasName="alias/DoesExist", TargetKeyId=key_id)

    target_id = prefix
    if append_key_id:
        target_id += key_id

    resp = client.generate_data_key(KeyId=target_id, NumberOfBytes=32)
    resp.should.have.key("KeyId").equals(
        f"arn:aws:kms:us-east-1:123456789012:key/{key_id}"
    )


@mock_kms
def test_generate_data_key_without_plaintext_decrypt():
    client = boto3.client("kms", region_name="us-east-1")
    key = client.create_key(Description="generate-data-key-decrypt")

    resp1 = client.generate_data_key_without_plaintext(
        KeyId=key["KeyMetadata"]["KeyId"], KeySpec="AES_256"
    )

    assert "Plaintext" not in resp1


@pytest.mark.parametrize("number_of_bytes", [12, 44, 91, 1, 1024])
@mock_kms
def test_generate_random(number_of_bytes):
    client = boto3.client("kms", region_name="us-west-2")

    response = client.generate_random(NumberOfBytes=number_of_bytes)

    response["Plaintext"].should.be.a(bytes)
    len(response["Plaintext"]).should.equal(number_of_bytes)


@pytest.mark.parametrize(
    "number_of_bytes,error_type",
    [(2048, botocore.exceptions.ClientError), (1025, botocore.exceptions.ClientError)],
)
@mock_kms
def test_generate_random_invalid_number_of_bytes(number_of_bytes, error_type):
    client = boto3.client("kms", region_name="us-west-2")

    with pytest.raises(error_type):
        client.generate_random(NumberOfBytes=number_of_bytes)


@mock_kms
def test_enable_key_rotation_key_not_found():
    client = boto3.client("kms", region_name="us-east-1")

    with pytest.raises(client.exceptions.NotFoundException):
        client.enable_key_rotation(KeyId="12366f9b-1230-123d-123e-123e6ae60c02")


@mock_kms
def test_disable_key_rotation_key_not_found():
    client = boto3.client("kms", region_name="us-east-1")

    with pytest.raises(client.exceptions.NotFoundException):
        client.disable_key_rotation(KeyId="12366f9b-1230-123d-123e-123e6ae60c02")


@mock_kms
def test_enable_key_key_not_found():
    client = boto3.client("kms", region_name="us-east-1")

    with pytest.raises(client.exceptions.NotFoundException):
        client.enable_key(KeyId="12366f9b-1230-123d-123e-123e6ae60c02")


@mock_kms
def test_disable_key_key_not_found():
    client = boto3.client("kms", region_name="us-east-1")

    with pytest.raises(client.exceptions.NotFoundException):
        client.disable_key(KeyId="12366f9b-1230-123d-123e-123e6ae60c02")


@mock_kms
def test_cancel_key_deletion_key_not_found():
    client = boto3.client("kms", region_name="us-east-1")

    with pytest.raises(client.exceptions.NotFoundException):
        client.cancel_key_deletion(KeyId="12366f9b-1230-123d-123e-123e6ae60c02")


@mock_kms
def test_schedule_key_deletion_key_not_found():
    client = boto3.client("kms", region_name="us-east-1")

    with pytest.raises(client.exceptions.NotFoundException):
        client.schedule_key_deletion(KeyId="12366f9b-1230-123d-123e-123e6ae60c02")


@mock_kms
def test_get_key_rotation_status_key_not_found():
    client = boto3.client("kms", region_name="us-east-1")

    with pytest.raises(client.exceptions.NotFoundException):
        client.get_key_rotation_status(KeyId="12366f9b-1230-123d-123e-123e6ae60c02")


@mock_kms
def test_get_key_policy_key_not_found():
    client = boto3.client("kms", region_name="us-east-1")

    with pytest.raises(client.exceptions.NotFoundException):
        client.get_key_policy(
            KeyId="12366f9b-1230-123d-123e-123e6ae60c02", PolicyName="default"
        )


@mock_kms
def test_list_key_policies_key_not_found():
    client = boto3.client("kms", region_name="us-east-1")

    with pytest.raises(client.exceptions.NotFoundException):
        client.list_key_policies(KeyId="12366f9b-1230-123d-123e-123e6ae60c02")


@mock_kms
def test_put_key_policy_key_not_found():
    client = boto3.client("kms", region_name="us-east-1")

    with pytest.raises(client.exceptions.NotFoundException):
        client.put_key_policy(
            KeyId="00000000-0000-0000-0000-000000000000",
            PolicyName="default",
            Policy="new policy",
        )


@pytest.mark.parametrize("id_or_arn", ["KeyId", "Arn"])
@mock_kms
def test_get_key_policy(id_or_arn):
    client = boto3.client("kms", region_name="us-east-1")
    key = client.create_key(Description="key1", Policy="my awesome key policy")
    key_id = key["KeyMetadata"][id_or_arn]

    # Straight from the docs:
    #   PolicyName: Specifies the name of the key policy. The only valid name is default .
    # But.. why.
    response = client.get_key_policy(KeyId=key_id, PolicyName="default")
    response["Policy"].should.equal("my awesome key policy")


@pytest.mark.parametrize("id_or_arn", ["KeyId", "Arn"])
@mock_kms
def test_put_key_policy(id_or_arn):
    client = boto3.client("kms", region_name="us-east-1")
    key_id = create_simple_key(client, id_or_arn)

    client.put_key_policy(KeyId=key_id, PolicyName="default", Policy="policy 2.0")

    response = client.get_key_policy(KeyId=key_id, PolicyName="default")
    response["Policy"].should.equal("policy 2.0")


@mock_kms
def test_put_key_policy_using_alias_shouldnt_work():
    client = boto3.client("kms", region_name="us-east-1")
    key_id = create_simple_key(client, policy="my policy")
    client.create_alias(AliasName="alias/my-alias", TargetKeyId=key_id)

    with pytest.raises(ClientError) as ex:
        client.put_key_policy(
            KeyId="alias/my-alias", PolicyName="default", Policy="policy 2.0"
        )
    err = ex.value.response["Error"]
    err["Code"].should.equal("NotFoundException")
    err["Message"].should.equal("Invalid keyId alias/my-alias")

    response = client.get_key_policy(KeyId=key_id, PolicyName="default")
    response["Policy"].should.equal("my policy")


@mock_kms
def test_list_key_policies():
    client = boto3.client("kms", region_name="us-east-1")
    key_id = create_simple_key(client)

    policies = client.list_key_policies(KeyId=key_id)
    policies["PolicyNames"].should.equal(["default"])


@pytest.mark.parametrize(
    "reserved_alias",
    ["alias/aws/ebs", "alias/aws/s3", "alias/aws/redshift", "alias/aws/rds"],
)
@mock_kms
def test__create_alias__raises_if_reserved_alias(reserved_alias):
    client = boto3.client("kms", region_name="us-east-1")
    key_id = create_simple_key(client)

    with pytest.raises(ClientError) as ex:
        client.create_alias(AliasName=reserved_alias, TargetKeyId=key_id)
    err = ex.value.response["Error"]
    err["Code"].should.equal("NotAuthorizedException")
    err["Message"].should.equal("")


@pytest.mark.parametrize(
    "name", ["alias/my-alias!", "alias/my-alias$", "alias/my-alias@"]
)
@mock_kms
def test__create_alias__raises_if_alias_has_restricted_characters(name):
    client = boto3.client("kms", region_name="us-east-1")
    key_id = create_simple_key(client)

    with pytest.raises(ClientError) as ex:
        client.create_alias(AliasName=name, TargetKeyId=key_id)
    err = ex.value.response["Error"]
    err["Code"].should.equal("ValidationException")
    err["Message"].should.equal(
        f"1 validation error detected: Value '{name}' at 'aliasName' failed to satisfy constraint: Member must satisfy regular expression pattern: ^[a-zA-Z0-9:/_-]+$"
    )


@mock_kms
def test__create_alias__raises_if_alias_has_restricted_characters_semicolon():
    # Similar test as above, but with different error msg
    client = boto3.client("kms", region_name="us-east-1")
    key_id = create_simple_key(client)

    with pytest.raises(ClientError) as ex:
        client.create_alias(AliasName="alias/my:alias", TargetKeyId=key_id)
    err = ex.value.response["Error"]
    err["Code"].should.equal("ValidationException")
    err["Message"].should.equal(
        "alias/my:alias contains invalid characters for an alias"
    )


@pytest.mark.parametrize("name", ["alias/my-alias_/", "alias/my_alias-/"])
@mock_kms
def test__create_alias__accepted_characters(name):
    client = boto3.client("kms", region_name="us-east-1")
    key_id = create_simple_key(client)

    client.create_alias(AliasName=name, TargetKeyId=key_id)


@mock_kms
def test__create_alias__raises_if_target_key_id_is_existing_alias():
    client = boto3.client("kms", region_name="us-east-1")
    key_id = create_simple_key(client)
    name = "alias/my-alias"

    client.create_alias(AliasName=name, TargetKeyId=key_id)

    with pytest.raises(ClientError) as ex:
        client.create_alias(AliasName=name, TargetKeyId=name)
    err = ex.value.response["Error"]
    err["Code"].should.equal("ValidationException")
    err["Message"].should.equal("Aliases must refer to keys. Not aliases")


@mock_kms
def test__create_alias__raises_if_wrong_prefix():
    client = boto3.client("kms", region_name="us-east-1")
    key_id = create_simple_key(client)

    with pytest.raises(ClientError) as ex:
        client.create_alias(AliasName="wrongprefix/my-alias", TargetKeyId=key_id)
    err = ex.value.response["Error"]
    err["Code"].should.equal("ValidationException")
    err["Message"].should.equal("Invalid identifier")


@mock_kms
def test__create_alias__raises_if_duplicate():
    client = boto3.client("kms", region_name="us-east-1")
    key_id = create_simple_key(client)
    alias = "alias/my-alias"

    client.create_alias(AliasName=alias, TargetKeyId=key_id)

    with pytest.raises(ClientError) as ex:
        client.create_alias(AliasName=alias, TargetKeyId=key_id)
    err = ex.value.response["Error"]
    err["Code"].should.equal("AlreadyExistsException")
    err["Message"].should.equal(
        f"An alias with the name arn:aws:kms:us-east-1:{ACCOUNT_ID}:alias/my-alias already exists"
    )


@mock_kms
def test__delete_alias():
    client = boto3.client("kms", region_name="us-east-1")

    key_id = create_simple_key(client)
    client.create_alias(AliasName="alias/a1", TargetKeyId=key_id)

    key_id = create_simple_key(client)
    client.create_alias(AliasName="alias/a2", TargetKeyId=key_id)

    client.delete_alias(AliasName="alias/a1")

    # we can create the alias again, since it has been deleted
    client.create_alias(AliasName="alias/a1", TargetKeyId=key_id)


@mock_kms
def test__delete_alias__raises_if_wrong_prefix():
    client = boto3.client("kms", region_name="us-east-1")

    with pytest.raises(ClientError) as ex:
        client.delete_alias(AliasName="wrongprefix/my-alias")
    err = ex.value.response["Error"]
    err["Code"].should.equal("ValidationException")
    err["Message"].should.equal("Invalid identifier")


@mock_kms
def test__delete_alias__raises_if_alias_is_not_found():
    client = boto3.client("kms", region_name="us-east-1")

    with pytest.raises(ClientError) as ex:
        client.delete_alias(AliasName="alias/unknown-alias")
    err = ex.value.response["Error"]
    err["Code"].should.equal("NotFoundException")
    err["Message"].should.equal(
        f"Alias arn:aws:kms:us-east-1:{ACCOUNT_ID}:alias/unknown-alias is not found."
    )


def sort(lst):
    return sorted(lst, key=lambda d: d.keys())


def _check_tags(key_id, created_tags, client):
    result = client.list_resource_tags(KeyId=key_id)
    actual = result.get("Tags", [])
    assert sort(created_tags) == sort(actual)

    client.untag_resource(KeyId=key_id, TagKeys=["key1"])

    actual = client.list_resource_tags(KeyId=key_id).get("Tags", [])
    expected = [{"TagKey": "key2", "TagValue": "value2"}]
    assert sort(expected) == sort(actual)


@mock_kms
def test_key_tag_on_create_key_happy():
    client = boto3.client("kms", region_name="us-east-1")

    tags = [
        {"TagKey": "key1", "TagValue": "value1"},
        {"TagKey": "key2", "TagValue": "value2"},
    ]
    key = client.create_key(Description="test-key-tagging", Tags=tags)
    _check_tags(key["KeyMetadata"]["KeyId"], tags, client)


@mock_kms
def test_key_tag_on_create_key_on_arn_happy():
    client = boto3.client("kms", region_name="us-east-1")

    tags = [
        {"TagKey": "key1", "TagValue": "value1"},
        {"TagKey": "key2", "TagValue": "value2"},
    ]
    key = client.create_key(Description="test-key-tagging", Tags=tags)
    _check_tags(key["KeyMetadata"]["Arn"], tags, client)


@mock_kms
def test_key_tag_added_happy():
    client = boto3.client("kms", region_name="us-east-1")

    key_id = create_simple_key(client)
    tags = [
        {"TagKey": "key1", "TagValue": "value1"},
        {"TagKey": "key2", "TagValue": "value2"},
    ]
    client.tag_resource(KeyId=key_id, Tags=tags)
    _check_tags(key_id, tags, client)


@mock_kms
def test_key_tag_added_arn_based_happy():
    client = boto3.client("kms", region_name="us-east-1")

    key_id = create_simple_key(client)
    tags = [
        {"TagKey": "key1", "TagValue": "value1"},
        {"TagKey": "key2", "TagValue": "value2"},
    ]
    client.tag_resource(KeyId=key_id, Tags=tags)
    _check_tags(key_id, tags, client)


@pytest.mark.parametrize("plaintext", PLAINTEXT_VECTORS)
@mock_kms
def test_sign_happy(plaintext):
    client = boto3.client("kms", region_name="us-west-2")

    key = client.create_key(Description="sign-key", KeyUsage="SIGN_VERIFY")
    key_id = key["KeyMetadata"]["KeyId"]
    key_arn = key["KeyMetadata"]["Arn"]
    signing_algorithm = "RSASSA_PSS_SHA_256"

    sign_response = client.sign(
        KeyId=key_id, Message=plaintext, SigningAlgorithm=signing_algorithm
    )

    sign_response["Signature"].should_not.equal(plaintext)
    sign_response["SigningAlgorithm"].should.equal(signing_algorithm)
    sign_response["KeyId"].should.equal(key_arn)


@mock_kms
def test_sign_invalid_signing_algorithm():
    client = boto3.client("kms", region_name="us-west-2")

    key = client.create_key(Description="sign-key", KeyUsage="SIGN_VERIFY")
    key_id = key["KeyMetadata"]["KeyId"]

    message = "My message"
    signing_algorithm = "INVALID"

    with pytest.raises(ClientError) as ex:
        client.sign(KeyId=key_id, Message=message, SigningAlgorithm=signing_algorithm)
    err = ex.value.response["Error"]
    err["Code"].should.equal("ValidationException")
    err["Message"].should.equal(
        "1 validation error detected: Value 'INVALID' at 'SigningAlgorithm' failed to satisfy constraint: Member must satisfy enum value set: ['RSASSA_PKCS1_V1_5_SHA_256', 'RSASSA_PKCS1_V1_5_SHA_384', 'RSASSA_PKCS1_V1_5_SHA_512', 'RSASSA_PSS_SHA_256', 'RSASSA_PSS_SHA_384', 'RSASSA_PSS_SHA_512']"
    )


@mock_kms
def test_sign_and_verify_ignoring_grant_tokens():
    client = boto3.client("kms", region_name="us-west-2")

    key = client.create_key(Description="sign-key", KeyUsage="SIGN_VERIFY")
    key_id = key["KeyMetadata"]["KeyId"]

    message = "My message"
    signing_algorithm = "RSASSA_PSS_SHA_256"

    sign_response = client.sign(
        KeyId=key_id,
        Message=message,
        SigningAlgorithm=signing_algorithm,
        GrantTokens=["my-ignored-grant-token"],
    )

    sign_response["Signature"].should_not.equal(message)

    verify_response = client.verify(
        KeyId=key_id,
        Message=message,
        Signature=sign_response["Signature"],
        SigningAlgorithm=signing_algorithm,
        GrantTokens=["my-ignored-grant-token"],
    )

    verify_response["SignatureValid"].should.equal(True)


@mock_kms
def test_sign_and_verify_digest_message_type_256():
    client = boto3.client("kms", region_name="us-west-2")

    key = client.create_key(Description="sign-key", KeyUsage="SIGN_VERIFY")
    key_id = key["KeyMetadata"]["KeyId"]

    digest = hashes.Hash(hashes.SHA256())
    digest.update(b"this works")
    digest.update(b"as well")
    message = digest.finalize()
    signing_algorithm = "RSASSA_PSS_SHA_256"

    sign_response = client.sign(
        KeyId=key_id,
        Message=message,
        SigningAlgorithm=signing_algorithm,
        MessageType="DIGEST",
    )

    verify_response = client.verify(
        KeyId=key_id,
        Message=message,
        Signature=sign_response["Signature"],
        SigningAlgorithm=signing_algorithm,
    )

    verify_response["SignatureValid"].should.equal(True)


@mock_kms
def test_sign_invalid_key_usage():
    client = boto3.client("kms", region_name="us-west-2")

    key = client.create_key(Description="sign-key", KeyUsage="ENCRYPT_DECRYPT")
    key_id = key["KeyMetadata"]["KeyId"]

    message = "My message"
    signing_algorithm = "RSASSA_PSS_SHA_256"

    with pytest.raises(ClientError) as ex:
        client.sign(KeyId=key_id, Message=message, SigningAlgorithm=signing_algorithm)
    err = ex.value.response["Error"]
    err["Code"].should.equal("ValidationException")
    err["Message"].should.equal(
        f"1 validation error detected: Value '{key_id}' at 'KeyId' failed to satisfy constraint: Member must point to a key with usage: 'SIGN_VERIFY'"
    )


@mock_kms
def test_sign_invalid_message():
    client = boto3.client("kms", region_name="us-west-2")

    key = client.create_key(Description="sign-key", KeyUsage="SIGN_VERIFY")
    key_id = key["KeyMetadata"]["KeyId"]

    message = ""
    signing_algorithm = "RSASSA_PSS_SHA_256"

    with pytest.raises(ClientError) as ex:
        client.sign(KeyId=key_id, Message=message, SigningAlgorithm=signing_algorithm)
    err = ex.value.response["Error"]
    err["Code"].should.equal("ValidationException")
    err["Message"].should.equal(
        "1 validation error detected: Value at 'Message' failed to satisfy constraint: Member must have length greater than or equal to 1"
    )


@pytest.mark.parametrize("plaintext", PLAINTEXT_VECTORS)
@mock_kms
def test_verify_happy(plaintext):
    client = boto3.client("kms", region_name="us-west-2")

    key = client.create_key(Description="sign-key", KeyUsage="SIGN_VERIFY")
    key_id = key["KeyMetadata"]["KeyId"]
    key_arn = key["KeyMetadata"]["Arn"]
    signing_algorithm = "RSASSA_PSS_SHA_256"

    sign_response = client.sign(
        KeyId=key_id, Message=plaintext, SigningAlgorithm=signing_algorithm
    )

    signature = sign_response["Signature"]

    verify_response = client.verify(
        KeyId=key_id,
        Message=plaintext,
        Signature=signature,
        SigningAlgorithm=signing_algorithm,
    )

    verify_response["SigningAlgorithm"].should.equal(signing_algorithm)
    verify_response["KeyId"].should.equal(key_arn)
    verify_response["SignatureValid"].should.equal(True)


@mock_kms
def test_verify_happy_with_invalid_signature():
    client = boto3.client("kms", region_name="us-west-2")

    key = client.create_key(Description="sign-key", KeyUsage="SIGN_VERIFY")
    key_id = key["KeyMetadata"]["KeyId"]
    key_arn = key["KeyMetadata"]["Arn"]
    signing_algorithm = "RSASSA_PSS_SHA_256"

    verify_response = client.verify(
        KeyId=key_id,
        Message="my test",
        Signature="invalid signature",
        SigningAlgorithm=signing_algorithm,
    )

    verify_response["SigningAlgorithm"].should.equal(signing_algorithm)
    verify_response["KeyId"].should.equal(key_arn)
    verify_response["SignatureValid"].should.equal(False)


@mock_kms
def test_verify_invalid_signing_algorithm():
    client = boto3.client("kms", region_name="us-west-2")

    key = client.create_key(Description="sign-key", KeyUsage="SIGN_VERIFY")
    key_id = key["KeyMetadata"]["KeyId"]

    message = "My message"
    signature = "any"
    signing_algorithm = "INVALID"

    with pytest.raises(ClientError) as ex:
        client.verify(
            KeyId=key_id,
            Message=message,
            Signature=signature,
            SigningAlgorithm=signing_algorithm,
        )
    err = ex.value.response["Error"]
    err["Code"].should.equal("ValidationException")
    err["Message"].should.equal(
        "1 validation error detected: Value 'INVALID' at 'SigningAlgorithm' failed to satisfy constraint: Member must satisfy enum value set: ['RSASSA_PKCS1_V1_5_SHA_256', 'RSASSA_PKCS1_V1_5_SHA_384', 'RSASSA_PKCS1_V1_5_SHA_512', 'RSASSA_PSS_SHA_256', 'RSASSA_PSS_SHA_384', 'RSASSA_PSS_SHA_512']"
    )


@mock_kms
def test_verify_invalid_message():
    client = boto3.client("kms", region_name="us-west-2")

    key = client.create_key(Description="sign-key", KeyUsage="SIGN_VERIFY")
    key_id = key["KeyMetadata"]["KeyId"]
    signing_algorithm = "RSASSA_PSS_SHA_256"

    with pytest.raises(ClientError) as ex:
        client.verify(
            KeyId=key_id,
            Message="",
            Signature="a signature",
            SigningAlgorithm=signing_algorithm,
        )

    err = ex.value.response["Error"]
    err["Code"].should.equal("ValidationException")
    err["Message"].should.equal(
        "1 validation error detected: Value at 'Message' failed to satisfy constraint: Member must have length greater than or equal to 1"
    )


@mock_kms
def test_verify_empty_signature():
    client = boto3.client("kms", region_name="us-west-2")

    key = client.create_key(Description="sign-key", KeyUsage="SIGN_VERIFY")
    key_id = key["KeyMetadata"]["KeyId"]

    message = "My message"
    signing_algorithm = "RSASSA_PSS_SHA_256"
    signature = ""

    with pytest.raises(ClientError) as ex:
        client.verify(
            KeyId=key_id,
            Message=message,
            Signature=signature,
            SigningAlgorithm=signing_algorithm,
        )
    err = ex.value.response["Error"]
    err["Code"].should.equal("ValidationException")
    err["Message"].should.equal(
        "1 validation error detected: Value at 'Signature' failed to satisfy constraint: Member must have length greater than or equal to 1"
    )


def create_simple_key(client, id_or_arn="KeyId", description=None, policy=None):
    with mock.patch.object(rsa, "generate_private_key", return_value=""):
        params = {}
        if description:
            params["Description"] = description
        if policy:
            params["Policy"] = policy
        return client.create_key(**params)["KeyMetadata"][id_or_arn]
