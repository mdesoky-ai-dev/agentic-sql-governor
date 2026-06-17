import pytest
from pydantic_ai import ModelRetry
from governor.firewall import validate_query

SAFE_QUERIES = [
    "SELECT id, email FROM users LIMIT 10",
    "SELECT country, COUNT(*) FROM users GROUP BY country LIMIT 50",
    "SELECT a FROM t1 UNION SELECT b FROM t2 LIMIT 10",
    "SELECT u.id, b.available_balance FROM users u "
    "JOIN balances b ON b.account_id = u.id WHERE u.country = 'US' LIMIT 100",
]

BLOCKED_QUERIES = [
    ("stacked statements",        "SELECT 1; DROP TABLE users"),
    ("drop table",                "DROP TABLE users"),
    ("delete rows",               "DELETE FROM users"),
    ("update rows",               "UPDATE accounts SET balance = 0 WHERE id = 1"),
    ("insert rows",               "INSERT INTO users (id) VALUES (1)"),
    ("truncate",                  "TRUNCATE TABLE users"),
    ("hidden delete in CTE",      "WITH t AS (DELETE FROM users RETURNING *) SELECT * FROM t LIMIT 10"),
    ("select into creates table", "SELECT * INTO copy_tbl FROM users LIMIT 10"),
    ("missing limit",             "SELECT * FROM users"),
    ("limit too high",            "SELECT * FROM users LIMIT 100000"),
    ("non-integer limit",         "SELECT * FROM users LIMIT 'x'"),
    ("pg_sleep DoS",              "SELECT pg_sleep(10) LIMIT 1"),
    ("pg_read_file file read",    "SELECT pg_read_file('/etc/passwd') LIMIT 1"),
    ("dblink network",            "SELECT * FROM dblink('host=evil', 'SELECT 1') AS t(x int) LIMIT 5"),
    ("garbage",                   "this is not sql at all !!!"),
    ("empty",                     "   "),
]

@pytest.mark.parametrize("query", SAFE_QUERIES)
def test_safe_queries_pass(query):
    assert validate_query(query) is not None

@pytest.mark.parametrize("label, query", BLOCKED_QUERIES)
def test_blocked_queries_raise(label, query):
    with pytest.raises(ModelRetry):
        validate_query(query)