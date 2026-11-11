# MakersLink-Scheduling
Project to develop a system to schecdule and book opening times at MakersLink

Rough roadmap:
1. Notification to email/slack
2. CSV-export?

## Deploying

The image is built on your own machine and copied to the server as a file.
The server only loads it and runs it with docker-compose.

**1. Build the image** (on your machine)

```sh
git pull && git fetch --tags
git tag $VERSION_NUM && git push origin $VERSION_NUM   # or use ./updateVersion.sh
sudo docker build -t makerslink_scheduling:$VERSION_NUM .
```

**2. Compress it and copy it to the server**

```sh
sudo docker save makerslink_scheduling:$VERSION_NUM | gzip > scheduling-$VERSION_NUM.tar.gz
scp scheduling-$VERSION_NUM.tar.gz somebody@server:/a/dir
```

**3. Load it on the server**

```sh
sudo docker load -i scheduling-$VERSION_NUM.tar.gz   # reads the .gz directly
sudo docker images makerslink_scheduling              # the new version is listed
```

**4. Run it with docker-compose**

The first time on a server: put `docker-compose.yml` and `.env.example` in a
folder, copy `.env.example` to `.env`, fill it in and `chmod 600 .env`. The
container runs as uid 10001, so the data directories must be writable by it:

```sh
mkdir -p data/db data/pks                # or point SCHEDULING_DB_DIR / SCHEDULING_PKS_DIR elsewhere
sudo chown -R 10001:10001 data/db data/pks
```

Then, the first time and for every update, set `SCHEDULING_IMAGE` in `.env` to
the version you loaded and run:

```sh
sudo docker-compose up -d
sudo docker-compose logs -f   # migrations, then gunicorn's "Listening at"
```

A beta server next to production only needs a different `.env`, for example
`SCHEDULING_CONTAINER=scheduling-beta`, `SCHEDULING_PORT=8001`,
`SCHEDULING_DB_DIR=/path/scheduling-beta/db` and
`SCHEDULING_PKS_DIR=/path/scheduling-beta/pks`.

- **Back up before updating production**: `sudo cp <db dir>/db.sqlite3 <db dir>/db.sqlite3.$(date +%F).back`.
  New versions can migrate the database, and the old image needs the old copy.
- **Roll back** by setting `SCHEDULING_IMAGE` to the previous version, restoring
  that backup and running `sudo docker-compose up -d` again.
- **Traefik** reaches the app on `SCHEDULING_PORT`. Docker opens that port on
  all of the server's addresses and ufw rules do not apply to it, so make sure
  it cannot be reached from outside, or anyone can use the site over plain HTTP.

## Local development

The database is not in git; create a local one with `python manage.py migrate`.
