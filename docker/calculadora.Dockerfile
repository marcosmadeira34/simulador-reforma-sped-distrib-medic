# Constrói a Calculadora RTC (regime regular) a partir do repositório oficial da Receita Federal.
# RTC_REF fixa a tag da calculadora (resultados reproduzíveis). Mantenha igual ao docker-compose.yml
# e a [calculadora] versao_esperada nas configurações.
FROM maven:3.9-eclipse-temurin-21 AS build
ARG RTC_REF=1.6.1
RUN git clone --depth 1 --branch ${RTC_REF} https://github.com/ReceitaFederal/calculadora-rtc-regime-regular.git /src
WORKDIR /src
RUN mkdir -p calculadora/db \
 && mvn -q flyway:migrate -Dflyway.configFiles=./flyway/flyway-pro.conf --batch-mode --errors \
 && mvn -q clean package -DskipTests --batch-mode

FROM eclipse-temurin:21-jre
WORKDIR /app
COPY --from=build /src/target/api-regime-geral.jar ./api-regime-geral.jar
COPY --from=build /src/calculadora/db ./calculadora/db
EXPOSE 8080
ENTRYPOINT ["java", "-Dspring.profiles.active=offline", "-jar", "api-regime-geral.jar"]
