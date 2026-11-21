import { Form } from "@raycast/api";
export default function Command() {
  return <Form><Form.TextField id="merchant" title="Merchant" /><Form.TextField id="amount" title="Amount" /></Form>;
}
