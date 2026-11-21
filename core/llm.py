import re


class FallbackLlm:
    def complete(self, *, system: str, user: str, json: bool = False, **kwargs) -> str:
        if json:
            amount = re.search(r'(?i)k(?:es|sh)?\s*([\d,]+)', user)
            merchant = re.search(r'(?i)(?:to|at|for)\s+([A-Za-z][A-Za-z .]+)', user)
            return (
                '{"merchantName":'
                + ('"'+merchant.group(1).strip()+'"' if merchant else 'null')
                + ',"invoiceNumber":null,"amount":'
                + (str(int(amount.group(1).replace(',', ''))) if amount else 'null')
                + ',"currency":"KES","dueDate":null,"accountRef":null}'
            )
        return 'The payment requires confirmation before it can proceed.'
