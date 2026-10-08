FROM golang:1.24
WORKDIR /app
COPY . .
RUN go build -o status .
EXPOSE 8080
CMD ["./status"]
