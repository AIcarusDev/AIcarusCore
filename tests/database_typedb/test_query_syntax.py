from collections.abc import AsyncGenerator

import pytest
from typedb.driver import Driver, TransactionType

DATA = 'insert $p isa platform, has platform-uid "qq", has display-name "QQ";'
QUERY_SHOULD_FAIL = (
    'match $p isa platform, has platform-uid "qq"; $p has display-name $n; $p has fake-attr $f;'
)
QUERY_SHOULD_SUCCEED = (
    'match $p isa platform, has platform-uid "qq", has display-name $n; select $n;'
)


@pytest.fixture(scope="function")
async def setup_syntax_test_data(db_connection: Driver) -> AsyncGenerator[None, None]:
    """Set up test data for syntax testing by inserting a platform record.

    Parameters
    ----------
    db_connection : Driver
        The TypeDB driver connection instance.

    Yields:
    ------
    None
        This fixture yields control back to the test after setup.
    """
    with db_connection.transaction("aicarus_test_db", TransactionType.WRITE) as tx:
        tx.query(DATA).resolve()
        tx.commit()
    yield


@pytest.mark.usefixtures("setup_syntax_test_data")
@pytest.mark.asyncio
async def test_invalid_query_fails(db_connection: Driver) -> None:
    """Test that invalid queries with non-existent attributes raise TypeDBDriverException."""
    from typedb.common.exception import TypeDBDriverException

    with (
        pytest.raises(TypeDBDriverException),
        db_connection.transaction("aicarus_test_db", TransactionType.READ) as tx,
    ):
        list(tx.query(QUERY_SHOULD_FAIL).resolve())


@pytest.mark.usefixtures("setup_syntax_test_data")
@pytest.mark.asyncio
async def test_valid_query_succeeds(db_connection: Driver) -> None:
    """测试有效查询的成功执行."""
    with db_connection.transaction("aicarus_test_db", TransactionType.READ) as tx:
        results = list(tx.query(QUERY_SHOULD_SUCCEED).resolve().as_concept_rows())
    assert len(results) == 1
    assert results[0].get("n").as_attribute().get_value() == "QQ"
