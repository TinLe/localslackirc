FROM python:3.8-slim
RUN mkdir -p /localslackirc/slackclient && \
    adduser --group --system localslackirc && \
    apt-get update && apt-get -yq upgrade && \
    apt-get -yq install apt-utils procps psmisc netcat net-tools lsof sudo && \
    echo "root:password" | chpasswd && \
    echo "localslackirc:password" | chpasswd
COPY requirements.txt tokenfile /localslackirc/
RUN python3 -m pip install -r /localslackirc/requirements.txt a
COPY *.py /localslackirc/
COPY slackclient/*.py /localslackirc/slackclient/
USER localslackirc
ENTRYPOINT PYTHONPATH=/localslackirc python3 /localslackirc/irc.py -o -i "0.0.0.0" -t /localslackirc/tokenfile
