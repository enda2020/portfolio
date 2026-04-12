# Trade-Based Stock Portfolio Tracker for Japanese Tax Reporting

A web application for tracking a stock portfolio by logging individual trades. It is designed to help with Japanese tax reporting by using the required moving-average cost basis method.

## Features

- **Trade Logging**: Record individual buy/sell transactions with details like broker, fees, and currency exchange rates.
- **Portfolio Dashboard**:
    - Automatically calculates and displays a summary of your current holdings.
    - JPY-first view of Total Value, Unrealized P&L, and Realized P&L.
    - Line chart showing portfolio value history over time.
    - Interactive pie chart to visualize portfolio composition by JPY value, with filters for broker and currency.
- **Japanese Tax Reporting**:
    - Generates a detailed, year-end tax report for a selected financial year.
    - Calculates proceeds, acquisition costs, and profit/loss in JPY using the required moving-average method.
    - Provides a transparent, expandable breakdown for each sale, showing the exact data and calculations used.
- **Data Management**:
    - Bulk upload trades from a CSV file with robust validation.
    - Export all trades to a CSV file for mass editing or backup.
- **Live Market Data**: Fetches current stock prices and USD/JPY exchange rates from Yahoo Finance.
- **Performance & Production Ready**:
    - Market data is cached for 5 minutes to ensure fast page loads.
    - Dockerized with a multi-stage build, runs on a Gunicorn WSGI server, and includes a CI/CD pipeline for automated builds.
- **Privacy Mode**: All monetary values can be masked/unmasked using the eye icon in the navigation bar. This setting is saved in your browser.

## Getting Started

The recommended way to run this application is with Docker. This method ensures your database (`holdings.db`) is stored in a local `data/` folder for easy access and backup.

### Option 1: Docker Compose (Easiest)

This is the recommended way to run the application. It uses the pre-built image from Docker Hub and automatically uses the local `data/` directory for database storage.

1.  **Pull the latest image from Docker Hub:**
    ```bash
    docker-compose pull
    ```
2.  **Start the application:**
    ```bash
    docker-compose up -d
    ```

The application will be available at `http://localhost:5001`.

### Option 2: Manual Docker Commands

1.  **Build the image:**
    ```bash
    docker build -t portfolio-app .
    ```
2.  **Run the container:**
    ```bash
    # This command mounts your local `data` directory into the container.
    # For Linux/macOS/PowerShell
    docker run --name portfolio-container -d -p 5001:5001 -v "$(pwd)/data":/app/data portfolio-app

    # For Windows Command Prompt (CMD)
    docker run --name portfolio-container -d -p 5001:5001 -v "%cd%/data":/app/data portfolio-app
    ```

### Local Development (Without Docker)

1.  Create and activate a virtual environment:
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows: venv\Scripts\activate
    ```
2.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```
3.  Run the Flask development server:
    ```bash
    python app.py
    ```

## Automated Builds (CI/CD)

This repository is configured with a GitHub Actions workflow to automatically build and push the Docker image to Docker Hub whenever changes are pushed to the `main` branch. This ensures the `latest` tag on Docker Hub always corresponds to the latest version of the source code.

The workflow file is located at `.github/workflows/docker-publish.yml`.

## File Structure

```text
.
├── .github/workflows/     # GitHub Actions CI/CD workflows
│   └── docker-publish.yml # Workflow to auto-build and push to Docker Hub
├── app.py                 # Main Flask application
├── Dockerfile             # Instructions to build the Docker image
├── docker-compose.yml     # Docker Compose configuration
├── .dockerignore          # Files to exclude from the Docker build
├── requirements.txt       # Python dependencies
├── data/                  # Directory for persistent data (ignored by git)
│   └── holdings.db        # SQLite database (created on first run)
├── templates/             # HTML templates
│   └── ...
└── README.md              # This file
```


