FROM python:3.10-alpine
WORKDIR /usr/local
RUN pip install --no-cache-dir --upgrade pip
COPY . /usr/local
RUN pip install -r requirements.txt
ENTRYPOINT []
