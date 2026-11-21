import { getPreferenceValues, showHUD } from "@raycast/api";

export default async function Command() {
  const { apiBaseUrl, token } = getPreferenceValues<{apiBaseUrl: string; token: string}>();
  const response = await fetch(`${apiBaseUrl}/api/freeze/`, {
    method: "POST",
    headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json"},
  });
  await showHUD(response.ok ? "AutoPay payments frozen" : "Could not freeze payments");
}
