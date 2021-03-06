FROM ubuntu:14.04
MAINTAINER David Manthey <david.manthey@kitware.com>

ENV KWDEMO_READY FALSE

EXPOSE 8080

RUN apt-get update && apt-get install -y \
    libffi-dev \
    libpython-dev \
    python-pip \
    curl \
    git

# Get nodejs apt source
RUN curl -sL https://deb.nodesource.com/setup | sudo bash -
RUN apt-get update && apt-get install -y \
    nodejs

RUN npm install -g npm@next && \
    npm --version

RUN npm install -g grunt-cli

# Create a user
RUN adduser --disabled-password --gecos '' kwuser && \
    adduser kwuser sudo && \
    echo '%sudo ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

USER kwuser

WORKDIR /home/kwuser

RUN git config --global url."https://".insteadOf git://

RUN git clone git://github.com/girder/girder.git && \
#    cd girder && \
#    git checkout ac66979e7c092a35addec7979c09d2e9f3d7e473 && \
#    git reset --hard && \
    true

WORKDIR /home/kwuser/girder

RUN pip install --user \
    -r requirements.txt \
    -r plugins/geospatial/requirements.txt \
    -r plugins/metadata_extractor/requirements.txt \
    -r plugins/celery_jobs/requirements.txt \
    -r plugins/hdfs_assetstore/requirements.txt
#    -r requirements-dev.txt

RUN npm install --production
RUN grunt init && grunt

COPY makelocalcfg.py /home/kwuser/makelocalcfg.py

# The environment variables beginning with KWDEMO can be used to map this demo
# to the main url space
ENV KWDEMO_NAME Girder Master Demo
ENV KWDEMO_KEY girdermaster
ENV KWDEMO_SRCURL https://github.com/girder/girder
ENV KWDEMO_DESC Girder is a free and open source web-based data management platform.  Girder is both a standalone application and a platform for building new web services.
ENV KWDEMO_IMG /girdermaster/static/img/Girder_Mark.png

RUN python /home/kwuser/makelocalcfg.py > girder/conf/girder.local.cfg

ENV KWDEMO_READY TRUE

CMD ["python", "-m", "girder"]
