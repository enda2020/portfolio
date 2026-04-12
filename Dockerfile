# Stage 1: Builder - Install dependencies with build tools
# We use a full 'buster' image because it includes the necessary compilers
# and development headers to build wheels for packages like numpy or lxml.
FROM python:3.11-buster as builder

WORKDIR /opt

# Create a virtual environment to keep dependencies isolated
RUN python -m venv venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy only the requirements file to leverage Docker's layer cache.
# This step is only re-run if requirements.txt changes.
COPY requirements.txt .

# Install python dependencies into the virtual environment
RUN pip install --no-cache-dir -r requirements.txt

# ---

# Stage 2: Final Image - The lightweight production image
FROM python:3.11-slim-buster

WORKDIR /app

# Copy the virtual environment from the builder stage.
COPY --from=builder /opt/venv /opt/venv

# Copy the application code from the local context into the container.
COPY . .

# Activate the virtual environment for subsequent commands.
ENV PATH="/opt/venv/bin:$PATH"

# Expose the port the app runs on
EXPOSE 5001

# Use Gunicorn as the production WSGI server.
# This command runs the app in production mode.
# We increase the timeout to 120 seconds to prevent worker timeouts when fetching
# prices for many stocks from the Yahoo Finance API on page load.
CMD ["gunicorn", "--bind", "0.0.0.0:5001", "--timeout", "120", "app:app"]