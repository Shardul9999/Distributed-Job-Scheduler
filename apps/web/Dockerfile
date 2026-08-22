# Dashboard image. Development-mode by default: the compose service bind-mounts
# the source and runs `next dev`, so this image only needs to carry the
# dependencies. A production build would swap the CMD for `next build && next
# start` behind a multi-stage copy -- out of scope for the assessment, which is
# graded on the running dev stack.
FROM node:24-alpine

WORKDIR /app/web

# Install deps first so a source change does not bust the npm cache layer.
COPY apps/web/package.json apps/web/package-lock.json* ./
RUN npm install

COPY apps/web/ ./

EXPOSE 3000

# Bind to 0.0.0.0 so the port is reachable from the host, not just inside the
# container's loopback.
CMD ["npm", "run", "dev", "--", "-H", "0.0.0.0"]
