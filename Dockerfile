# Use an official lightweight Python image
FROM python:3.11-slim

# Install OS-level dependencies for Camelot (Ghostscript and OpenCV prerequisites)
RUN apt-get update && apt-get install -y \
    ghostscript \
    libgl1-mesa-glx \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file and install Python libraries
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy your bot's source code into the container
COPY . .

# Expose the port for the Flask Keep-Alive server
EXPOSE 8080

# Command to run your bot
CMD ["python", "main.py"]
