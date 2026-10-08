# syntax=docker/dockerfile:1
FROM golang:1.24 AS build
WORKDIR /src
COPY go.mod ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/status .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/status /status
USER nonroot:nonroot
EXPOSE 8080
ENTRYPOINT ["/status"]
