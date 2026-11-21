import { Action, ActionPanel, List, getPreferenceValues } from "@raycast/api";

type Preferences = { apiBaseUrl: string; token: string };

export default function Command() {
  const preferences = getPreferenceValues<Preferences>();
  return (
    <List>
      <List.Item
        title="Open AutoPay"
        subtitle={preferences.apiBaseUrl}
        actions={
          <ActionPanel>
            <Action.OpenInBrowser url={preferences.apiBaseUrl} />
          </ActionPanel>
        }
      />
    </List>
  );
}
