from app.learning_models import TutorAnalysis


def verified_linear_analysis(
    analysis: TutorAnalysis, problem: str, steps: list[str]
) -> TutorAnalysis:

    from app.math_check import check_linear

    checked = check_linear(problem, steps)
    if checked and checked.status == "error":
        from app.skills import linear_error_skill

        analysis = analysis.model_copy(
            update={"skill": linear_error_skill(problem, steps, checked.first_error_step)}
        )
    if checked is None or (
        analysis.status == checked.status
        and analysis.first_error_step == checked.first_error_step
        and analysis.complete == checked.complete
    ):
        return analysis
    incorrect = checked.status == "error"
    return TutorAnalysis(
        status=checked.status,
        first_error_step=checked.first_error_step,
        complete=checked.complete,
        skill=analysis.skill if incorrect else "Уравнения",
        question=(
            "Одинаковое ли действие выполнено с обеими частями уравнения?"
            if incorrect
            else "Как можно самостоятельно проверить выполненные преобразования?"
        ),
        hint=(
            "Сравни обе части до и после действия: одинаковое ли действие выполнено с ними?"
            if incorrect
            else "Сохраняй равенство, выполняя одинаковое допустимое действие с обеими частями."
        ),
        detail=(
            "При переносе слагаемого можно вычесть его из обеих частей равенства. "
            "При делении делитель должен быть одинаковым и ненулевым. "
            "Проверь вычисления на выбранном действии. Сохраняется ли исходное равенство?"
            if incorrect
            else "Проверенные действия сохраняют множество решений уравнения. "
            "Для самопроверки можно подставить собственный результат в исходное условие. "
            "Сходятся ли обе части?"
        ),
        discovery=(
            "Самостоятельно выбери первое ошибочное действие, исправь его и объясни правило."
            if incorrect
            else "Объясни, почему выполненные тобой преобразования сохраняют равенство."
        ),
    )
