import re
def classify(symbol: str, patterns: dict):
    for name, pattern in patterns.items():
        try:
            if re.search(pattern, symbol, flags=re.IGNORECASE):
                return name
        except re.error:
            continue
    return symbol

