# NEON WALLET

Native Android wallet project built with Kotlin and Jetpack Compose.

## Project contents
- Android app with a dark purple NEON WALLET interface
- Kotlin business-rule unit tests
- FastAPI backend with PostgreSQL
- Render Blueprint for a free-tier test deployment
- GitHub Actions workflow for building a debug APK

## Important
The backend stores **virtual test balances only**. It is not connected to banks, payment providers, real cards, deposits, or cash withdrawals. The current Android interface still contains local prototype/demo balance behavior; API integration is a separate next development step.

## Android build
Open the project in Android Studio or use **Actions → Build Android APK → Run workflow**. The workflow runs unit tests and assembles a debug APK.

## Backend
See [server/README.md](server/README.md) for API endpoints and local run instructions. `render.yaml` defines the test API and PostgreSQL resources. Render's free PostgreSQL plan is temporary (30 days); do not store important user data there.

## Repository history
The previous NEON SLOTS Telegram Mini App source is preserved in the branch `archive/neon-slots-casino-before-wallet`.
