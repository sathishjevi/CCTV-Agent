# Vendor (sub-processor) register

| Vendor | Role | Data they receive | Status | To do |
|---|---|---|---|---|
| Railway | Hosting, Postgres | Everything the service stores | In use | DPA, confirm region, enable backups |
| Google Firebase (FCM) | Push delivery | Push token, notification text | In use | DPA, restrict API key |
| PostHog | Error tracking | Error-level logs, truncated IPs, no passwords/tokens | In use | DPA, confirm region (US/EU host) |
| Twilio (or configured SMS provider) | SMS | Phone numbers, short task text | In use | DPA |
| Anthropic / Voyage AI | AI chat agent | Staff questions and retrieved snippets | Deferred | DPA and data review before enabling |
| GitHub | Source, CI | Code only, no production data | In use | Enable secret scanning/push protection |

Review at least yearly and whenever a vendor is added.
