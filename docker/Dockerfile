FROM python:3.11
WORKDIR /app
COPY ../requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ../sen-bashi.py .
EXPOSE 5001
# Add the mock directory to the system PATH. This allows subprocess.run() to find the scripts.
ENV PATH="/opt/mock:$PATH"
CMD ["python", "sen-bashi.py"]
