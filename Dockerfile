FROM python:3.10
WORKDIR /usr/local
RUN pip install --no-cache-dir --upgrade pip
COPY . /usr/local
RUN pip install -r requirements.txt
RUN python client.py monitor /dev/ttyUSB0 -o packet.log
